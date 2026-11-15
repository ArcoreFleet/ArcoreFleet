# Arcore Portal — users, app access, app tiles

**https://portal.example.com** (also **https://arcorefleet.example.com**)
— a small Django app on K3s:

- **Tiles** for every app the signed-in user can open.
- **Users & access** (admins): a user × app checkbox grid, an **All apps**
  checkbox per user (every app, including ones added later), Admin/Active
  toggles, "add user by email" to grant access before a first sign-in, and a
  **New users get all apps** default.
- **Applications** (admins): register apps by hostname, with name,
  description, icon and color for their tile.
- **Operations** (admins): dated workflow, K3s rollout, image revision and
  BrowserForge test observations per environment, plus CPU, memory and disk
  snapshots for Oracle and Tailscale-connected homelab servers. Choose a 5/10-minute
  interval or request **Poll now** for all servers or one host. See
  [Operations](OPERATIONS.md) for configuration and the optional collector.
- **Website** (admins): review queue for example.com pull requests, CI
  results, Vercel previews and blog posts. See [Website manager](WEBSITE.md).
- **API keys** (everyone; admins see and can revoke all): personal keys for
  the [Fleet MCP gateway](../fleet-mcp/README.md). A key acts as its owner,
  within the owner's app access. Only a SHA-256 hash is stored; the key is
  shown once. The gateway resolves keys through `POST /api/keys/resolve`,
  which requires `FLEET_MCP_SHARED_SECRET` (in `portal-env`) and refuses
  everything when it is unset.

Sign-in is Cloudflare Access (Google/Microsoft): the portal verifies the
`Cf-Access-Jwt-Assertion` JWT and signs the user in by email - no separate
login. Emails in `CF_ACCESS_ADMIN_EMAILS` become admins on first sign-in.

## Giving someone access

Two gates, in this order:

1. **Cloudflare Zero Trust** - may this person sign in at all? Add their
   email (or domain) to the allow policy of the `ArcoreFleet Apps` Access
   application (`*.example.com`). Until then they get Cloudflare's
   "not authorized" page and never reach the portal.
2. **The portal** - which apps may they open? Admins, on **Users & access**:
   pre-add the email (or wait for their first sign-in, which creates them),
   then tick apps or **All apps**. Takes effect within 15 seconds.

The Access policy is managed by Terraform
(`phase1-network-ingress/cloudflare-access`, `allowed_emails` /
`allowed_email_domains`). Emails added only in the Zero Trust dashboard are
removed by the next `terraform apply` - add them to `terraform.tfvars` too.

## How access is enforced

```
browser ─► Cloudflare Access (Google/Microsoft; is this person allowed on *.example.com at all?)
        ─► Tunnel ─► Traefik ──forwardAuth──► portal /auth/check  (may THIS user open THIS app?)
                        │                         200 → continue to the app
                        └──► app                  403 → "No access" page instead
```

- An app opts in with one Ingress annotation (the Phase 4 template has it):
  `traefik.ingress.kubernetes.io/router.middlewares: portal-auth@file`.
  The middleware is defined in
  `phase1-network-ingress/traefik/dynamic/portal-auth.yml` and calls the
  portal through NodePort 30480 on fleet-control.
- `/auth/check` verifies the Access JWT itself, looks up the app by
  `X-Forwarded-Host`, and allows: **admins** - everything; **users** - apps
  granted to them, or every active app with *All apps*. A hostname that isn't
  registered refuses everyone but admins. First-time users are created
  (with the *New users* default) so they show up in the grid.
- Decisions are cached ~15 s per worker, so access changes apply within
  15 seconds. `/auth/check` never creates sessions.
- **If the portal is down, gated apps refuse requests** (fail closed).

## Deploy

Data: database `portal` on the Phase 3 Postgres (via
`phase4-app-template/provision-app.sh portal`, so it's on masterdb too).

```bash
# Secrets (once): portal-db comes from provision-app.sh; portal-env:
kubectl -n apps create secret generic portal-env \
  --from-literal=DJANGO_SECRET_KEY=... --from-literal=CF_ACCESS_ADMIN_EMAILS=you@example.com
kubectl apply -f k3s/portal.yaml
cp ../phase1-network-ingress/traefik/dynamic/portal-auth.yml ~/arcorefleet/traefik/dynamic/   # on fleet-control
```

Then every push to `main` touching `portal/` builds and rolls it out
(`docs/examples/workflows/portal.yml`, Phase 5 pipeline). CI may only update the
Deployment's image (Phase 5 `ci-deployer` role), so changes to
`k3s/portal.yaml` itself (hostnames, env, Services) need a manual
`kubectl apply -f k3s/portal.yaml`; run it after a CI build, since the file
pins `:latest`.

## Local development

```bash
pip install -r requirements.txt
python manage.py migrate && python manage.py createsuperuser
DJANGO_DEBUG=true python manage.py runserver   # sign in at /admin/, then /
python manage.py test access website
```

## Website manager

Admins review website pull requests, CI results, Vercel previews and blog posts
for example.com at `/website/`. A separate worker holds the GitHub App
credential; the web pods never call GitHub for it. See [Website manager](WEBSITE.md)
for setup.

## Admin-only applications

Enable **Admin only** in an application's settings for infrastructure tools
such as Portainer. Only active users with **Admin** checked in **Users & access**
can see its tile or pass `/auth/check` for its hostname. **All apps** and specific
app grants do not override this restriction. All routed paths, including APIs
and WebSocket handshakes, must use the `portal-auth@file` middleware.
Portainer additionally retains its own login. See
[Portainer routing](../phase2-k3s-portainer/README.md).

## ArcoreHub authority (development)

The portal is also the **authority** for [ArcoreHub](https://github.com/ArcoreFleet/ArcoreHub), the Arcore desktop host. ArcoreHub trusts exactly one authority, through a chain it verifies itself:

```
root key (offline; its public half is built into ArcoreHub)
  -> delegation: this portal's configuration key may sign for authority arcore-dev
       -> configuration: control-plane endpoints, TLS pins, version
```

- **`/hub/` (everyone):** download the signed `.arcoreprofile`. It holds only public settings and does nothing until the user signs in from ArcoreHub.
- **`/hub/` (admins):** publish configuration (endpoints, pins, tenant, validity). Each publish signs the next version, and ArcoreHub never accepts an older one. Code: `hubauthority/`.
- **Control plane (`k3s/hub-authority.yaml`):** what ArcoreHub fetches `/v1/config` from. It runs the same image with `PORTAL_ROLE=hub-authority`, and gunicorn terminates TLS with the key whose pin the configuration lists. It holds no signing key. It's tailnet only, because Cloudflare's certificates on the public hostnames can't be pinned.

### One-time setup

Key material is made with `arcore-trust` from the ArcoreHub repository (`cargo build -p arcore-trust-cli`). The root key never leaves the owner's machine. Only the configuration key, the delegation and the TLS key go to the cluster.

```bash
arcore-trust keygen --out config.key              # prints {key_id, public_key}
arcore-trust delegate --root root.key --config-public <public_key> --authority arcore-dev --env dev --days 90 > delegation.json
arcore-trust tls-cert --host fleet-control.example-tailnet.ts.net --days 397 --cert tls.crt --key tls.key   # prints the pin
```

Add the signing material to `portal-env`, and create the TLS Secret:

```bash
kubectl -n apps patch secret portal-env --type merge -p "{\"stringData\":{\"HUB_CONFIG_KEY\":\"$(cat config.key)\",\"HUB_CONFIG_DELEGATION\":$(jq -Rs . < delegation.json)}}"
kubectl -n apps create secret tls hub-authority-tls --cert=tls.crt --key=tls.key
kubectl apply -f portal/k3s/hub-authority.yaml
kubectl -n apps rollout restart deployment/portal
```

Then, on `/hub/`, publish the endpoint `fleet-control.example-tailnet.ts.net:30443` with the pin `arcore-trust tls-cert` printed.

- **Delegations expire,** after 90 days above. The page shows the date. Re-run `delegate`, update `HUB_CONFIG_DELEGATION`, then publish again.
- **Rotating the TLS key:** publish a configuration listing both the old and the new pin first, then switch the Secret.

