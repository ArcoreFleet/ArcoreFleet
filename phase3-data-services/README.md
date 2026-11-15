# Phase 3 — Centralized database & registry services

All three services are pinned to `fleet-control` (the always-available node),
same rationale as Portainer in Phase 2: their PVCs are node-bound
(`local-path` storage class), so the pod must always land on the node
that actually has the data.

## Deploy order

```bash
kubectl apply -f namespace.yaml

cp postgres/secret.example.yaml postgres/secret.yaml
# edit postgres/secret.yaml with real values (never commit secret.yaml)
kubectl apply -f postgres/secret.yaml -f postgres/pvc.yaml -f postgres/deployment.yaml -f postgres/service.yaml

cp redis/secret.example.yaml redis/secret.yaml
# edit redis/secret.yaml with a real value (never commit secret.yaml)
kubectl apply -f redis/secret.yaml -f redis/pvc.yaml -f redis/deployment.yaml -f redis/service.yaml

kubectl apply -f registry/pvc.yaml -f registry/deployment.yaml -f registry/service.yaml
```

## Postgres as primary for app databases (replicated to masterdb)

Postgres runs with `wal_level=logical` (see `postgres/deployment.yaml`) so
each app database can be logically replicated to `masterdb` on fleet-worker -
see [`../masterdb/README.md`](../masterdb/README.md). Per app (ArcoreEstate
is set up this way), as the superuser via `kubectl exec`:

```sql
CREATE ROLE <app> LOGIN PASSWORD '...';
CREATE DATABASE <app> OWNER <app>;
REVOKE ALL ON DATABASE <app> FROM PUBLIC;
GRANT CONNECT ON DATABASE <app> TO masterdb_repl;
\c <app>
CREATE PUBLICATION masterdb_pub FOR ALL TABLES;
```

`masterdb_repl` (created once: `LOGIN REPLICATION`, `GRANT pg_read_all_data`)
is the only role masterdb's subscriptions use. `max_slot_wal_keep_size=2GB`
stops a long-offline fleet-worker from filling the 10Gi PVC with retained WAL.

## Access

| Service | In-cluster address | Tailnet address (NodePort) |
|---|---|---|
| Postgres | `postgres.data.svc.cluster.local:5432` | `<fleet-control-tailscale-ip>:30432` |
| Redis | `redis.data.svc.cluster.local:6379` | in-cluster only (no NodePort) |
| Registry | `registry.data.svc.cluster.local:5000` | `<fleet-control-tailscale-ip>:30500` |

None of these have a public route through Traefik/Cloudflare — same
tailnet-only model as Portainer. A database and an image registry have no
business being reachable from the public internet.

## Registry: trusting plain HTTP

The registry has no TLS certificate (it's internal-only, reachable solely
over the tailnet), so every K3s node needs to be told to trust it over
plain HTTP before it can pull images from it. Run on **every** node
(server and agents):

```bash
./registry/configure-node.sh <fleet-control-tailscale-ip>:30500
```

This writes `/etc/rancher/k3s/registries.yaml` and restarts the local
k3s/k3s-agent service. Without this, pulling an image like
`<registry-ip>:30500/myapp:latest` fails with a TLS handshake error.

Pushing from a plain Docker host (not through K3s) works automatically
when pushing to `localhost:30500` from the registry's own node — Docker
treats `127.0.0.0/8`/`localhost` as insecure by default. Pushing from any
other host (e.g. a CI runner later in Phase 5) needs that host's Docker
configured with `"insecure-registries": ["<fleet-control-tailscale-ip>:30500"]`
in `/etc/docker/daemon.json`, then `sudo systemctl restart docker`.
