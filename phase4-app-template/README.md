# Phase 4 — Standardized multi-arch app template

## Prerequisite: wiring Traefik up to K3s

Phase 1's standalone Traefik (plain Docker, outside the cluster) had no
visibility into K3s at all - it only watched Docker labels and static
files. For "deploy an app, it just gets routed" to actually work, Traefik
needs to watch the K3s API directly for standard `Ingress` objects.

```bash
kubectl apply -f traefik-k8s-provider/rbac.yaml
kubectl apply -f traefik-k8s-provider/ingressclass.yaml
./traefik-k8s-provider/extract-credentials.sh ~/arcorefleet/traefik
cd ~/arcorefleet/traefik && docker compose up -d  # recreates the container with the new config
```

This grants a read-only ServiceAccount access to watch Ingresses/Services/
Endpoints/Secrets/Nodes, pulls its token + the cluster CA cert, and
writes the real `traefik.yml` (gitignored) from the committed
`traefik.yml.template`.

### Two non-obvious things this needed, found by testing against the real cluster

1. **The token has to live in the config file, not a CLI flag or env
   var.** Traefik does not deep-merge a single leaf of a provider block
   across different config sources - if `endpoint` and
   `certAuthFilePath` come from the file, a CLI-flag-only `token` is
   silently dropped and the request goes out unauthenticated
   (`"the server has asked for the client to provide credentials"`).
   Putting the whole block, including the token, in the file fixed it
   immediately. This is why `traefik.yml.template` has a
   `__K8S_SA_TOKEN__` placeholder instead of the token coming from
   `docker-compose.yml`.
2. **An `IngressClass` object named `traefik` must actually exist.**
   Setting `ingressClass: traefik` in Traefik's static config isn't
   enough by itself - Traefik's provider resolves that name against a
   real `IngressClass` resource to confirm it's meant for Traefik. With
   no such object in the cluster, every Ingress using
   `ingressClassName: traefik` was silently ignored (no error logged at
   all - it just never became a router). Fixed with:
   ```bash
   kubectl apply -f - <<'EOF'
   apiVersion: networking.k8s.io/v1
   kind: IngressClass
   metadata:
     name: traefik
   spec:
     controller: traefik.io/ingress-controller
   EOF
   ```
3. Traefik also needed `network_mode: host` in `docker-compose.yml` - the
   default Docker bridge network can't reliably reach the host's own
   `tailscale0` interface IP (a known Docker+Tailscale interaction), and
   the Docker-label provider got disabled entirely (an unrelated
   Traefik<->Docker API version mismatch) since nothing in this project
   actually used Docker-label routing.

## Adding a new app

1. **Data layer, one command** (run on fleet-worker):
   `./provision-app.sh <app>` - creates the app's own login + database on
   the shared Phase 3 Postgres (never the superuser), the `apps/<app>-db`
   Secret with its `POSTGRES_*` settings, and its standby copy on masterdb.
   `--no-standby` skips masterdb.
2. Copy `k3s/deployment-template.yaml`, fill in every `<PLACEHOLDER>`. It
   loads `<app>-db` via envFrom and opts the Ingress in to per-user access
   checks (`portal-auth@file`).
3. First time only: `kubectl apply -f k3s/namespace.yaml` and
   `./k3s/copy-shared-secrets.sh` (Redis only - Postgres credentials are
   per app now).
4. `kubectl apply -f <your-app>.yaml`
5. Register the app in the portal (https://portal.example.com ->
   Applications, hostname = the Ingress host) and grant users access. Until
   then only admins get through.

For repeatable portal/SSO registration, add the app to
[`sso/apps.json`](../sso/README.md) and use the Fleet SSO provisioning workflow.
Use `cloudflare_jwt` for apps with verified Cloudflare-token middleware, or
`oidc` for apps with native OAuth/OIDC login. Keep `portal-auth@file` on the
Ingress; an OIDC client alone does not enforce portal access grants.

To make the app usable through the [Fleet MCP gateway](../fleet-mcp/README.md)
(one portal API key for every app), accept its `X-Arcore-Fleet-Token` and
add the app to `fleet-mcp/fleet_mcp/apps.json` - see "Adding an app" there.

It's reachable at `https://<hostname>.example.com` within seconds - no
Traefik file, no DNS change, no tunnel change. The wildcard CNAME and
Cloudflare Access policy from Phase 1 already cover any hostname under the
root domain.

Verified end-to-end with a real test app (`traefik/whoami`): deployed
with nothing but a Deployment + Service + Ingress, and it was reachable
through Traefik immediately, then through the full public path
(Cloudflare Access gate -> Tunnel -> Traefik) with no additional steps.

For an app that genuinely can't run in K3s, see
`docker-compose/docker-compose.yml.template` instead - routing for those
has to be added manually as a Traefik dynamic file (Docker-label
discovery is currently disabled; see above).

## Resource limits

Every template - K3s and Compose - sets `resources.limits` /
`mem_limit`+`cpus`. This is not optional: it's what stops one
misbehaving app from starving the node or the rest of the fleet, the
exact failure mode "50+ apps on shared hardware" is most exposed to.
Tune the numbers to the app's real footprint, but never remove the caps.
