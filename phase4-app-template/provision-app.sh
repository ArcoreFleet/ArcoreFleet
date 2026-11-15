#!/usr/bin/env bash
# Provisions everything an app needs on the shared data layer, in one step:
#
#   1. Phase 3 Postgres (fleet-control): login <app> + database <app> owned by it,
#      PUBLIC revoked, masterdb_repl allowed to connect, publication for
#      replication.
#   2. K3s: Secret "<app>-db" in the apps namespace (POSTGRES_DB/USER/
#      PASSWORD/HOST/PORT), which the Phase 4 template loads via envFrom.
#   3. masterdb (fleet-worker): database + apps/<app>/ folder, replica.env, and a
#      subscription - so the standby copy follows the primary from day one.
#      The app's first migration is picked up automatically by masterdb's
#      check/resync (see masterdb/README.md).
#
# Run on fleet-worker (where masterdb lives); reaches fleet-control over ssh.
#
#   ./provision-app.sh <app> [--no-standby]
set -euo pipefail

APP="${1:-}"
STANDBY=true; [ "${2:-}" = "--no-standby" ] && STANDBY=false
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_HOST="${ARCORE01_SSH:-fleet-control}"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "[provision-app] $*"; }

[[ "$APP" =~ ^[a-z][a-z0-9_]{1,62}$ ]] || die "usage: $0 <app> [--no-standby]  (app: ^[a-z][a-z0-9_]{1,62}\$)"
[ -f "$ROOT/masterdb/.env" ] || die "missing $ROOT/masterdb/.env"
set -a; . "$ROOT/masterdb/.env"; set +a
: "${ARCORE01_REPL_PASSWORD:?set ARCORE01_* in masterdb/.env}"

kube() { ssh "$SSH_HOST" sudo k3s kubectl "$@"; }
# psql as the Phase 3 superuser inside the postgres pod; SQL on stdin.
# Optional $1: extra psql flags (e.g. -tA for bare query output).
phase3_psql() {
  ssh "$SSH_HOST" "sudo k3s kubectl -n data exec -i deploy/postgres -- \
    sh -c 'psql -X -q ${1:-} -v ON_ERROR_STOP=1 -U \"\$POSTGRES_USER\" -d postgres'"
}

[ "$(echo "SELECT count(*) FROM pg_roles WHERE rolname = '$APP';" | phase3_psql -tA | tr -d '[:space:]')" = 0 ] \
  || die "login '$APP' already exists on Phase 3 Postgres"
[ -e "$ROOT/masterdb/apps/$APP" ] && die "masterdb/apps/$APP already exists"

# Alphanumeric only, so it can be inlined into SQL and env files safely.
PW="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32)"

log "Phase 3 Postgres: login + database '$APP'"
phase3_psql <<SQL
CREATE ROLE "$APP" LOGIN PASSWORD '$PW';
CREATE DATABASE "$APP" OWNER "$APP";
REVOKE ALL ON DATABASE "$APP" FROM PUBLIC;
GRANT CONNECT ON DATABASE "$APP" TO masterdb_repl;
\c "$APP"
CREATE PUBLICATION masterdb_pub FOR ALL TABLES;
SQL

log "K3s: Secret apps/$APP-db"
kube apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Secret
metadata:
  name: $APP-db
  namespace: apps
type: Opaque
stringData:
  POSTGRES_DB: "$APP"
  POSTGRES_USER: "$APP"
  POSTGRES_PASSWORD: "$PW"
  POSTGRES_HOST: "postgres.data.svc.cluster.local"
  POSTGRES_PORT: "5432"
YAML

if $STANDBY; then
  log "masterdb: standby database + subscription"
  "$ROOT/masterdb/masterdb" add-app "$APP" >/dev/null
  ( umask 077
    cat > "$ROOT/masterdb/apps/$APP/replica.env" <<EOF
# masterdb holds a logical replica of this app; its primary lives here.
PRIMARY_NAME=fleet-control
PRIMARY_HOST=$ARCORE01_PG_HOST
PRIMARY_PORT=$ARCORE01_PG_PORT
PRIMARY_DB=$APP
# Replication login (REPLICATION + pg_read_all_data) - used by the subscription.
REPL_USER=$ARCORE01_REPL_USER
REPL_PASSWORD=$ARCORE01_REPL_PASSWORD
# Database owner on the primary - used by failback to copy data back.
APP_USER=$APP
APP_PASSWORD=$PW
EOF
  )
  "$ROOT/masterdb/masterdb" resync "$APP" >/dev/null
fi

# Local credential reference (secrets/ is gitignored).
mkdir -p "$ROOT/secrets"
cat >> "$ROOT/secrets/phase3-data-services.md" <<EOF

## $APP (provisioned $(date -u +%F) by provision-app.sh)
- Database \`$APP\` on the Phase 3 Postgres, owner login \`$APP\`
- Password: \`$PW\`
- K3s Secret: \`apps/$APP-db\`; standby on masterdb: $STANDBY
EOF

cat <<EOF

Done. Next:
  - In the app's Deployment: envFrom secretRef "$APP-db" (k3s/deployment-template.yaml already does).
  - Deploy; the app's migrations create its tables on fleet-control$($STANDBY && echo ", and masterdb copies them automatically (check runs every 5 min)").
  - Register the app in the portal (https://portal.example.com/manage/apps/) and grant users access.
EOF
