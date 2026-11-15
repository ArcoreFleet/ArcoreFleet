#!/bin/sh
# Run as the existing Docker operator. No password, token or database credentials.
set -eu
bind_ip=${1:?Usage: install.sh TAILSCALE_IP}
case "$bind_ip" in 100.*) ;; *) echo 'Use this host Tailscale IPv4 address' >&2; exit 1;; esac
tailscale ip -4 | grep -Fx "$bind_ip" >/dev/null
deployment_dir="$HOME/.local/share/fleet-metrics/deployment"
textfile_dir="$HOME/.local/share/fleet-metrics/textfile"
mkdir -p "$deployment_dir" "$textfile_dir"
chmod 755 "$textfile_dir"
if docker inspect fleet-node-exporter >/dev/null 2>&1; then
  owner=$(docker inspect -f '{{index .Config.Labels "com.arecore.fleet.component"}}' fleet-node-exporter)
  [ "$owner" = host-metrics ] || { echo 'Existing container is not managed by Fleet' >&2; exit 1; }
fi
image=quay.io/prometheus/node-exporter:v1.12.1
docker pull "$image" >/dev/null
digest=$(docker image inspect -f '{{index .RepoDigests 0}}' "$image")
case "$digest" in quay.io/prometheus/node-exporter@sha256:*) ;; *) echo 'Missing immutable image digest' >&2; exit 1;; esac
cp "$(dirname "$0")/docker-compose.yml" "$deployment_dir/docker-compose.yml"
printf 'FLEET_NODE_EXPORTER_IMAGE=%s\nFLEET_METRICS_BIND_IP=%s\nFLEET_TEXTFILE_DIR=%s\n' "$digest" "$bind_ip" "$textfile_dir" > "$deployment_dir/.env"
docker compose -p fleet-host-metrics -f "$deployment_dir/docker-compose.yml" --env-file "$deployment_dir/.env" up -d
echo "Configured private metrics on $bind_ip:9100"
