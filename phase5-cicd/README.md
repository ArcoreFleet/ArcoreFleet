# Phase 5 — Multi-arch CI/CD pipeline

Application repositories build on GitHub-hosted runners and deploy over the tailnet.



## Why this differs from the original plan

The original plan called for a Portainer webhook to "update the running
stack." That's the right mechanism for Portainer-managed Docker Compose
stacks - but Phase 2 onward moved app deployment to K3s-native
Deployments/Ingress, not Portainer stacks. For those, the real equivalent
of "update the running stack" is `kubectl set image` +
`kubectl rollout status` against the Deployment - that's what this
pipeline does. The Portainer-webhook mechanism is kept as documented
alternative (`portainer-webhook-optional.md`) for an app that genuinely
runs as Compose instead.

## One-time setup (per cluster, not per app)

1. **Tailscale OAuth client**, scoped to `tag:ci` only - lets ephemeral
   CI runners join the tailnet for the duration of a job, never a
   long-lived key. Requires the `tag:ci` entry in
   `phase1-network-ingress/tailscale/tailscale-acl.json` (already added)
   to be pasted into the Tailscale admin console's ACL policy first.

   **The required scope is "Auth Keys" (with Write), not "Devices
   Core."** This isn't obvious from the console UI and cost real time to
   find: a client scoped to Devices Core (even with Write + `tag:ci`
   selected in its own tag field) can list devices but gets a bare `404
   Not Found` from `POST /api/v2/tailnet/-/keys` - the exact same error
   `tailscale/github-action` surfaces as `Join tailnet` failing with no
   further detail. Under **Settings -> Keys -> Auth Keys** in the OAuth
   client's scope editor, check **Write**, then select `tag:ci` in the
   **"Tags (required for write scope)"** field that appears directly
   underneath it - that field belongs to Auth Keys, not Devices. Save,
   and the resulting token's scope should read `devices:core auth_keys`
   (verify with a direct curl to `/api/v2/oauth/token` if the Action
   fails again - the scope string is printed in the response).
2. **CI deployer RBAC** (least privilege - can only update Deployments in
   the `apps` namespace, nothing else):
   ```bash
   kubectl apply -f ci-rbac/rbac.yaml
   ./ci-rbac/generate-ci-kubeconfig.sh <server-tailscale-ip>
   ```
   The script prints a base64 kubeconfig - that's the `KUBE_CONFIG_CI`
   GitHub secret, shared across every app repo (it's scoped by namespace,
   not by app, so one is enough for all of them).

## Per-app setup (once per app repo)

In the app's own repository (not this infra repo):

1. Copy `github-actions/build-and-deploy.yml.template` to
   `.github/workflows/build-and-deploy.yml`, filling in
   `<REGISTRY_TAILSCALE_IP>` and `<APP_NAME>`.
2. Copy `github-actions/buildkitd.toml.template` to
   `phase5-buildkitd.toml` at the repo root, filling in the same registry
   IP - BuildKit refuses to push to a plain-HTTP registry without this,
   same reasoning as Phase 3's `configure-node.sh`.
3. Add repo secrets: `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_CLIENT_SECRET`,
   `KUBE_CONFIG_CI` (the same three values/secret for every app).
4. The app must already be deployed once via Phase 4's K3s template
   (`kubectl set image` updates an existing Deployment, it doesn't create
   one).

## What happens on every push to `main`

1. Runner joins the tailnet as an ephemeral `tag:ci` device
2. `buildx` builds the image for both `linux/amd64` and `linux/arm64` in
   one multi-arch manifest, pushed to the private registry over the
   tailnet
3. `kubectl set image` points the app's Deployment at the new tag, then
   waits for the rollout to finish

No GitHub-hosted-runner-reachable public registry is needed, and the
cluster's API is never exposed beyond the `tag:ci` -> `tag:cloud` grant on
ports 30500/6443 - CI can't reach anything else in the tailnet, including
Postgres, Redis, or Portainer.
