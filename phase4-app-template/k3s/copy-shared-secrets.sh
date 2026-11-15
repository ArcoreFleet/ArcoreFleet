#!/usr/bin/env bash
# Mirrors the shared Redis credentials (Phase 3, namespace "data") into
# the "apps" namespace, since Kubernetes Secrets are namespace-scoped - a
# Deployment in "apps" can't envFrom a Secret that lives in "data"
# directly. Run once; re-run if Phase 3's Redis password ever changes.
#
# Postgres is deliberately NOT copied: postgres-credentials is the
# superuser, and every app gets its own login and database instead (see
# ../provision-app.sh).
set -euo pipefail

for secret in redis-credentials; do
  kubectl get secret "${secret}" -n data -o json \
    | python3 -c "
import json, sys
obj = json.load(sys.stdin)
obj['metadata'] = {'name': obj['metadata']['name'], 'namespace': 'apps'}
print(json.dumps(obj))
" \
    | kubectl apply -f -
done

echo "Copied redis-credentials into the apps namespace."
