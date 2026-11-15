"""One-way masterdb (Postgres) -> Oracle ADB full-refresh feed for one app.

Postgres is the system of record; Oracle ArcoreDB01 (a Lakehouse/ADW
instance) holds a copy for analytics and as an offsite backup. Both sides
must already have the same schema, created by the app's own migrations
(`manage.py migrate` run against each database). This copies rows only:

  * Postgres is read inside a single REPEATABLE READ, READ ONLY transaction,
    so every table comes from the same point in time.
  * Oracle is written inside a single transaction (DELETE + INSERT, not
    TRUNCATE - TRUNCATE is DDL in Oracle and commits immediately), so
    analytics queries see either the old copy or the new one, never a
    half-finished feed. Django's FKs are DEFERRABLE INITIALLY DEFERRED,
    so table order doesn't matter inside that transaction.

It applies Django's Oracle-backend conventions along the way: identifiers
uppercased and truncated to 30 chars with an md5 suffix, booleans stored as
NUMBER(1), UUIDs as 32-char hex, timestamps as naive UTC.
"""

import argparse
import datetime
import decimal
import hashlib
import json
import os
import sys
import time
import uuid

import oracledb
import psycopg
from psycopg import sql

ALWAYS_EXCLUDE = {"django_migrations"}    # compared for drift, never copied
LOB_TYPES = {
    "CLOB": oracledb.DB_TYPE_CLOB,
    "NCLOB": oracledb.DB_TYPE_NCLOB,
    "BLOB": oracledb.DB_TYPE_BLOB,
}
BATCH = 2000


def log(msg):
    print(f"[sync] {msg}", flush=True)


def read_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


def django_oracle_name(name, length=30, hash_len=4):
    """Mirror django.db.backends.oracle quote_name(): truncate_name() then upper()."""
    if len(name) > length:
        digest = hashlib.md5(name.encode(), usedforsecurity=False).hexdigest()[:hash_len]
        name = name[: length - hash_len] + digest
    return name.upper()


def to_oracle(v):
    if v is None or isinstance(v, (str, int, float, decimal.Decimal, datetime.date, datetime.timedelta)):
        if isinstance(v, bool):
            return int(v)                                   # NUMBER(1)
        if isinstance(v, datetime.datetime) and v.tzinfo is not None:
            return v.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        if isinstance(v, datetime.time) and v.tzinfo is not None:
            return v.replace(tzinfo=None)
        return v
    if isinstance(v, uuid.UUID):
        return v.hex
    if isinstance(v, (bytes, memoryview)):
        return bytes(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v)                                # JSONField -> NCLOB IS JSON
    return v


def pg_tables(pg):
    rows = pg.execute(
        """SELECT table_name, column_name
             FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public')
            ORDER BY table_name, ordinal_position"""
    ).fetchall()
    tables = {}
    for table, col in rows:
        tables.setdefault(table, []).append(col)
    return tables


def ora_tables(ora):
    cur = ora.cursor()
    cur.execute("SELECT table_name, column_name, data_type FROM user_tab_columns ORDER BY table_name, column_id")
    tables = {}
    for table, col, dtype in cur:
        tables.setdefault(table, {})[col] = dtype
    return tables


def check_drift(ora, pg, pg_tbls, ora_tbls):
    if "django_migrations" not in pg_tbls:
        return
    if "DJANGO_MIGRATIONS" not in ora_tbls:
        raise SystemExit("schema drift: Oracle has no django_migrations - run the app's migrate against Oracle first")
    pg_migs = set(pg.execute("SELECT app, name FROM django_migrations").fetchall())
    cur = ora.cursor()
    cur.execute('SELECT "APP", "NAME" FROM "DJANGO_MIGRATIONS"')
    ora_migs = set(cur.fetchall())
    missing = sorted(pg_migs - ora_migs)
    if missing:
        shown = ", ".join(f"{a}.{n}" for a, n in missing[:5])
        raise SystemExit(
            f"schema drift: {len(missing)} migration(s) applied on masterdb but not on Oracle "
            f"({shown}{'...' if len(missing) > 5 else ''}). Run the app's migrate against Oracle first."
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("app")
    ap.add_argument("--force", action="store_true", help="skip the row-count safety guard")
    args = ap.parse_args()

    cfg = read_env(f"/apps/{args.app}/sync.env")
    exclude = ALWAYS_EXCLUDE | {t.strip().lower() for t in cfg.get("SYNC_EXCLUDE", "").split(",") if t.strip()}
    min_ratio = float(cfg.get("SYNC_MIN_RATIO", "0.5"))
    started = time.monotonic()

    pg = psycopg.connect(dbname=args.app)  # admin creds from PG* env
    pg.execute("SET TIME ZONE 'UTC'")
    pg.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

    ora = oracledb.connect(
        user=cfg["ORACLE_USER"],
        password=cfg["ORACLE_PASSWORD"],
        dsn=cfg.get("ORACLE_DSN", "arcoredb01_tp"),
        config_dir="/wallet",
        wallet_location="/wallet",
        wallet_password=os.environ["ORACLE_WALLET_PASSWORD"],
    )

    pg_tbls = pg_tables(pg)
    ora_tbls = ora_tables(ora)
    if not pg_tbls:
        log(f"{args.app}: masterdb has no tables yet - nothing to sync")
        return
    if not ora_tbls:
        raise SystemExit(f"{args.app}: Oracle has no tables - run the app's migrate against Oracle first")
    check_drift(ora, pg, pg_tbls, ora_tbls)

    plan = []
    for table, cols in pg_tbls.items():
        if table in exclude:
            continue
        ot = django_oracle_name(table)
        if ot not in ora_tbls:
            log(f"warn: {table} has no Oracle counterpart ({ot}) - skipped")
            continue
        present = [(c, django_oracle_name(c)) for c in cols if django_oracle_name(c) in ora_tbls[ot]]
        for c in cols:
            if django_oracle_name(c) not in ora_tbls[ot]:
                log(f"warn: {table}.{c} not in Oracle - not copied")
        plan.append((table, ot, present))
    known = {django_oracle_name(t) for t in pg_tbls}
    for ot in sorted(set(ora_tbls) - known):
        log(f"warn: Oracle table {ot} has no masterdb counterpart - left untouched")

    # Safety guard: refuse to wipe the Oracle copy with a suspiciously small source.
    src_total = sum(
        pg.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(t))).fetchone()[0]
        for t, _, _ in plan
    )
    cur = ora.cursor()
    dst_total = 0
    for _, ot, _ in plan:
        cur.execute(f'SELECT COUNT(*) FROM "{ot}"')
        dst_total += cur.fetchone()[0]
    if not args.force and dst_total and src_total < dst_total * min_ratio:
        raise SystemExit(
            f"guard: masterdb has {src_total} rows vs {dst_total} on Oracle (< {min_ratio:.0%}). "
            "Refusing to sync; rerun with --force if this is expected."
        )

    # One Oracle transaction: oracledb doesn't autocommit, and DELETE/INSERT are DML.
    for _, ot, _ in plan:
        cur.execute(f'DELETE FROM "{ot}"')
    for table, ot, cols in plan:
        lob_sizes = {i: LOB_TYPES[ora_tbls[ot][oc]] for i, (_, oc) in enumerate(cols) if ora_tbls[ot][oc] in LOB_TYPES}
        insert = 'INSERT INTO "{}" ({}) VALUES ({})'.format(
            ot, ", ".join(f'"{oc}"' for _, oc in cols), ", ".join(f":{i + 1}" for i in range(len(cols)))
        )
        select = sql.SQL("SELECT {} FROM {}").format(
            sql.SQL(", ").join(sql.Identifier(c) for c, _ in cols), sql.Identifier(table)
        )
        n = 0
        with pg.cursor(name=f"feed_{table}") as src:   # server-side cursor: bounded memory
            src.itersize = BATCH
            src.execute(select)
            while rows := src.fetchmany(BATCH):
                ins = ora.cursor()
                if lob_sizes:
                    ins.setinputsizes(*[lob_sizes.get(i) for i in range(len(cols))])
                ins.executemany(insert, [[to_oracle(v) for v in row] for row in rows])
                n += len(rows)
        if n:
            log(f"{table}: {n} rows")
    ora.commit()
    pg.rollback()

    # Move identity columns past the copied ids (DDL, so after the commit).
    cur = ora.cursor()
    cur.execute("SELECT table_name, column_name FROM user_tab_identity_cols")
    for ot, oc in cur.fetchall():
        ora.cursor().execute(f'ALTER TABLE "{ot}" MODIFY "{oc}" GENERATED BY DEFAULT ON NULL AS IDENTITY (START WITH LIMIT VALUE)')

    log(f"{args.app}: synced {len(plan)} tables, {src_total} rows to Oracle in {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str):
            log(f"ABORTED: {e.code}")
            sys.exit(1)
        raise
