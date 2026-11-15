# masterdb — multi-app standby Postgres + Oracle feed

A Postgres 16 instance (plain Docker Compose) on **fleet-worker**, the homelab
node, with one database per app. Every app has exactly **one home
(primary)** database; masterdb either *is* that home or keeps a live
replica of it, so masterdb always holds a current copy of every app. From
here, one job feeds every app's data to Oracle ArcoreDB01 (Lakehouse) for
analytics and an offsite copy.

```
           fleet-control (OCI, arm64)                    fleet-worker (homelab, amd64)
  ┌──────────────────────────────────┐   logical   ┌──────────────────────────┐
  │ K3s apps ─► Phase 3 Postgres     │ replication │ masterdb                 │
  │             (primary for         │ ──────────► │  replica of cloud apps   │
  │              cloud-homed apps)   │  Tailscale  │  primary of homelab apps │
  └──────────────────────────────────┘             └────────────┬─────────────┘
                                                                 │ masterdb sync (every 15 min)
                                                                 ▼
                                                   Oracle ADB ArcoreDB01 (analytics copy)
```

Tailnet-only, like Phase 3: the port is bound to fleet-worker's Tailscale IP
(`192.0.2.11:5432`), never `0.0.0.0`, and has no public route.

**Why logical replication, not streaming:** fleet-control is arm64 and fleet-worker is
amd64. Physical (streaming) replication copies raw data files and isn't
supported across CPU architectures; logical replication copies rows and
doesn't care. It is also per database, which matches one-database-per-app.

## Layout

```
masterdb/
  masterdb            CLI - everything below goes through it
  compose.yml         db (postgres:16) + sync (one-shot, profile "sync")
  .env                admin creds, bind address, wallet password, alerting (gitignored)
  oracle-wallet/      ADB wallet, copied from OracleDBTest (gitignored)
  sync/               masterdb -> Oracle feed (sync.py + Dockerfile)
  apps/<app>/         one folder per app
    .env              the app's masterdb creds, POSTGRES_* names (gitignored)
    replica.env       present when the app's primary is elsewhere (gitignored)
    sync.env          the app's Oracle target creds (gitignored)
    FAILED_OVER       present while masterdb is serving the app after a failover
    schema/           schema history - committed, diffable
    snapshots/        pg_dump -Fc data snapshots (gitignored)
  logs/               cron output (gitignored)
```

## Commands

| Command | What it does |
|---|---|
| `add-app <app>` | Creates role `<app>`, database `<app>` owned by it (PUBLIC revoked), and `apps/<app>/` with a generated password |
| `list` | Apps, database sizes, latest snapshot |
| `status` | Each app's role (`primary` / `replica` / `failed-over`) and seconds since the last message from its primary |
| `check` | Cron health check: alerts and exits 1 if a replica's subscription is disabled/missing or silent for > `CHECK_MAX_LAG_SECONDS` |
| `snapshot <app>\|--all` | Data snapshot `snapshots/<app>_<ts>.dump` + schema snapshot; keeps the newest `SNAPSHOT_KEEP` (14) |
| `schema <app>\|--all` | Schema only; a new `schema/<app>_<ts>.sql` is kept **only when the schema changed**, so `schema/` is a change history; `latest.sql` is current |
| `restore <app> <file.dump> [--yes]` | Safety snapshot, then restore in one transaction. Refused on a live replica |
| `replicate <app> [--yes]` | Subscribes to the primary in `replica.env`. **Empties local tables first** (after a safety snapshot), then copies everything |
| `refresh <app>` | Picks up tables added on the primary (without a full resync) |
| `resync <app>` | Rebuilds the replica from the primary's current schema + a full copy; `check` runs it automatically on replication errors |
| `failover <app> [--yes]` | Detaches from the primary (without needing it reachable), fixes sequences, marks the app `FAILED_OVER` |
| `failback <app> [--yes]` | Dumps the primary's stale data (safety), copies masterdb's data back, resumes replication |
| `sync <app>\|--all [--force]` | masterdb -> Oracle ADB feed (below) |
| `psql [app]` | Interactive psql as admin |

## Adding an app

**Homed on fleet-control (runs in K3s)** - like ArcoreEstate:

1. On the Phase 3 Postgres: create the app's role + database, `GRANT CONNECT ON
   DATABASE <app> TO masterdb_repl`, and in that database
   `CREATE PUBLICATION masterdb_pub FOR ALL TABLES` (see
   `phase3-data-services/README.md`).
2. Run the app's migrations against fleet-control.
3. `./masterdb add-app <app>`, write `apps/<app>/replica.env` (copy
   `apps/arcoreestate/replica.env`, change `PRIMARY_DB`/`APP_USER`/`APP_PASSWORD`),
   then `./masterdb resync <app>` - it copies the primary's schema and data.

**Homed on masterdb (homelab-only app):** `./masterdb add-app <app>`, migrate,
point the app at `apps/<app>/.env`. Done - no `replica.env`.

**Oracle feed (either kind):** create the app's Oracle user, run the app's
migrations against Oracle, then `apps/<app>/sync.env`:

```
ORACLE_DSN=arcoredb01_tp
ORACLE_USER=<oracle schema user>
ORACLE_PASSWORD=...
SYNC_EXCLUDE=django_session      # tables never copied
SYNC_MIN_RATIO=0.5               # safety guard, see below
```

## Schema changes (migrations)

Logical replication carries rows, not DDL, so a migration on the primary
breaks replication until masterdb has the same schema. That is handled
automatically:

1. The app migrates its primary (ArcoreEstate's backend does this on every
   deploy, and migrates its Oracle copy right after - `entrypoint.prod.sh`).
2. The next row change hits a missing column/table on masterdb and the
   subscription records an apply error.
3. `check` (every 5 min) sees the error and runs `resync`: safety snapshot,
   drop the subscription, rebuild masterdb's `public` schema from the
   primary's own `pg_dump --schema-only` (the app's migrations never run
   against masterdb), resubscribe with a full copy. Seconds for a small app.

So the standby lags by at most ~5 minutes plus the copy time across a
migration. `./masterdb resync <app>` does the same by hand. Set
`AUTO_RESYNC=false` in `.env` to only alert instead.

## masterdb -> Oracle feed

- **Full refresh, consistent and atomic.** Postgres is read in one
  `REPEATABLE READ` snapshot; Oracle is written in one transaction (`DELETE`
  + `INSERT` - `TRUNCATE` is DDL in Oracle and would commit mid-feed), so
  Oracle readers see the old copy or the new one, never a mix.
- **Django-on-Oracle conventions applied:** identifiers uppercased and
  truncated to 30 chars + md5 suffix, booleans as `NUMBER(1)`, UUIDs as hex,
  timestamps as naive UTC, JSON/long text into LOBs. Identity columns are
  moved past the copied ids afterwards.
- **Drift check:** if masterdb's `django_migrations` has entries Oracle
  doesn't, the feed aborts - migrate Oracle first.
- **Guard:** if masterdb has fewer than `SYNC_MIN_RATIO` x Oracle's rows, the
  feed aborts instead of wiping the Oracle copy. `--force` overrides once.
- Reads from masterdb, never from the primary - no load on fleet-control.

## Failover / failback

Failover is deliberately **manual** (one command), with automated
*detection*. fleet-control also runs the K3s control plane, Traefik and
cloudflared: if the whole node dies, the K3s apps die with it and there is
nothing to repoint; if only the Postgres pod dies, K3s restarts it. So an
auto-flipping watchdog has almost no case where it's both needed and able
to act. `check` (every 5 min) tells you when to act; set `NOTIFY_URL` (e.g.
an ntfy.sh topic) to get pushed alerts.

```bash
./masterdb failover arcoreestate     # masterdb becomes writable for the app
# point the app at masterdb: the POSTGRES_* values in apps/arcoreestate/.env
# ... later, with the app STOPPED:
./masterdb failback arcoreestate     # copies masterdb -> fleet-control, resumes replication
# point the app back at fleet-control and start it
```

Data loss on failover: only what fleet-control committed but hadn't yet streamed
(normally under a second). Anything written to fleet-control *after* the failover
(split-brain) is discarded by failback but kept in
`snapshots/<app>_<ts>_fleet-control_prefailback.dump`.

## Schedule (fleet-worker crontab)

```
*/5 * * * *   masterdb check           >> logs/check.log
*/15 * * * *  masterdb sync --all      >> logs/sync.log
15 3 * * *    masterdb snapshot --all  >> logs/snapshot.log
```

## Caveats

- **Boot order:** the port binds to the Tailscale IP, so Docker must start
  after `tailscaled` has its address. If fleet-worker reboots and `masterdb` is
  `Exited` with a bind error, `./masterdb up` fixes it.
- **WAL retention:** while masterdb is offline, fleet-control keeps WAL for its
  replication slot, capped at `max_slot_wal_keep_size=2GB`. Offline longer than
  that and the slot is invalidated - `check` alerts, and the fix is
  `masterdb failover` + `failback` (or drop the subscription and `replicate`
  again).
- `failback` prints a harmless `pg_dump` warning about circular foreign keys;
  Django's FKs are deferrable, so the single-transaction load handles them.

## Logical replication worker capacity

The standby reserves `max_logical_replication_workers=16` within
`max_worker_processes=24`. Each subscribed database consumes an apply worker,
and initial table copies need additional workers. With PostgreSQL's default
of four logical workers, the fourth app (ArcoreVox) connected but all its tables
remained in initialization because the existing apply workers occupied the pool.
These settings require a standby restart; app primaries continue running.
After onboarding an app, check that all its `pg_subscription_rel.srsubstate`
values reach `r`, rather than relying only on an enabled subscription.
