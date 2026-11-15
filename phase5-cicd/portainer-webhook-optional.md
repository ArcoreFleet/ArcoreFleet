# Optional: Portainer webhook path for Docker Compose apps

The K3s path above (`kubectl set image` + `rollout status`) is the
primary deploy mechanism - it's what every app following Phase 4's K3s
template uses. This doc is only for an app that genuinely runs as plain
Docker Compose instead (Phase 4's `docker-compose.yml.template`), where
Portainer's own webhook feature is the more natural fit.

Portainer can redeploy a stack it manages whenever a webhook URL is hit -
useful since Portainer already has Docker access on the node and doesn't
need the CI runner to reach anything beyond the registry.

## Setup

1. In Portainer, import the app as a **Stack** (Stacks -> Add stack ->
   paste the Compose file) rather than deploying it by hand with
   `docker compose up`.
2. On that stack's detail page, enable **Webhook** - Portainer generates
   a unique URL like `https://<portainer-host>/api/stacks/webhooks/<uuid>`.
3. Since Portainer is tailnet-only (Phase 2 - deliberately not public),
   the webhook URL is only reachable from the tailnet too. The CI step
   becomes:
   ```yaml
   - name: Trigger Portainer redeploy
     run: curl -X POST "http://<fleet-control-tailscale-ip>:30900/api/stacks/webhooks/<uuid>"
   ```
   (after the same `tailscale/github-action` join step used for the K3s
   path).
4. The stack's Compose file should reference `:latest` (or a fixed tag
   CI always re-pushes) so the webhook-triggered pull actually picks up
   the new image - a webhook hitting a stack pinned to an immutable tag
   like a specific SHA won't change anything.

This path does **not** get the least-privilege RBAC scoping the K3s path
gets - the webhook has whatever permissions the stack itself was deployed
with. Prefer the K3s path for anything that can run there.
