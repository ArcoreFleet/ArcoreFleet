# Phase 2 - K3s cluster and Portainer

## Cluster
- `fleet-control` (Oracle, arm64): K3s control plane and persistent Portainer data.
- `fleet-worker` (homelab, amd64): K3s agent over Tailscale.
- Fleet's existing standalone Traefik handles ingress.

## Portainer access

Portainer is already running. `ingress.yaml` exposes its existing service at
**https://portainer.example.com/** without adding a workload or replica.
Requests pass through the existing wildcard Cloudflare Access policy, Tunnel,
Traefik `portal-auth@file`, and Portainer's own login.

Before applying the Ingress, register this hostname in the portal with
**Admin only** enabled. Only active users whose **Admin** checkbox is selected
in **Users & access** can see the tile or pass the hostname authorization check.
**All apps** and individual app grants cannot override **Admin only**.
Authorization changes take effect within the portal's 15-second cache window.

Apply only after the portal's `0005_application_admin_only` migration:

```sh
kubectl apply -f phase2-k3s-portainer/ingress.yaml
```

Portainer retains its own credentials; the portal does not impersonate a
Portainer user. Initial setup times out after a few minutes without an admin
account. Restart the existing deployment to reopen setup if needed.

Native Cloudflare OAuth requires the Content-Type fallback included in the pinned
Portainer CE 2.45.2 release. Older releases such as 2.21.4 and 2.39.8 can reject
Cloudflare's userinfo response with `mime: invalid media parameter` even after
sign-in and token exchange succeed. See the
[upstream fix](https://github.com/portainer/portainer/blob/2.45.2/api/oauth/oauth.go).
Take a consistent backup of the existing PVC before upgrading; database migrations
mean rolling back the image alone is insufficient. Keep backup files private,
because the database contains authentication configuration and credentials.

The existing NodePorts (30900 HTTP, 30943 HTTPS, 30800 edge tunnel) remain
private infrastructure access and do not go through the portal. Do not expose
those ports through the public firewall. Portal Admin enforcement applies to
the public hostname; direct trusted network access retains Portainer's login.

Rollback: delete only this Ingress and deactivate the portal application.
Portainer and its persistent data keep running.
