# Application SSO provisioning

`apps.json` is the reviewed catalog for portal registration and SSO setup.
The provisioner runs only during CI or a manual invocation: no new server,
sidecar, periodic polling, or continuously running authentication service.

## What it automates

- `cloudflare_jwt`: register the portal tile and access restriction. Apps like
  ArcoreEstate still need their existing JWT-verifying application middleware;
  no new Cloudflare client is necessary under the existing wildcard gate.
- `oidc`: create/update a Cloudflare Access SaaS OIDC client, copy the existing
  wildcard's identity providers and explicit allow/deny policy conditions, and
  persist the client credentials in `fleet-sso/fleet-sso-<id>`.
- `adapter: portainer`: configure native OAuth using the saved client, enable
  SSO, and provision individual Portainer accounts for active portal admins.
  Automatic user creation is disabled. The portal Admin checkbox remains the
  authorization gate for every public page/API request, regardless of All apps.

The wildcard self-hosted Access application, DNS, tunnel, existing application
grants, and application workloads are not replaced. Removed catalog entries
are not deleted from Cloudflare, Kubernetes, or the portal. An OIDC integration
does not install application middleware, create an Ingress, or make an app public.
Use `portal-auth@file` on every application Ingress.

## One-time setup

1. Add an account-scoped Cloudflare API token as repository secret
   `CLOUDFLARE_API_TOKEN`. It needs **Access: Apps and Policies Edit** for the
   existing account. Set repository variable `CLOUDFLARE_ACCOUNT_ID` to that
   account's 32-character ID. No Google/Microsoft provider secrets are needed.
2. In namespace `portainer`, create Secret `portainer-sso-bootstrap` with
   `username` and `password` for the initial Portainer admin. Use a private file
   and stdin/file input; never put credentials in source, command arguments or
   an Actions log. The current deployment's initial admin credentials are in
   the private login file created during Portainer setup.
   Apply `sso/rbac.yaml` once with the infrastructure administrator identity.
   This creates a dedicated namespace for generated SSO credentials, grants CI
   get/create/patch there, get on only the named Portainer bootstrap Secret,
   and pod exec in `apps`. It grants no access to database namespace Secrets.
3. The included `docs/examples/workflows/provision-sso.yml` reuses the existing
   Tailscale and Kubernetes CI secrets; no new GitHub deployment credentials
   are needed.
4. Set repository variable `FLEET_SSO_ENABLED=true`. Until then, validation runs
   but the production apply job is skipped. Configure the `production` GitHub
   environment's protections as appropriate. No apply occurs on pull requests.
5. Run **Provision application SSO** once. Future merges changing `sso/**` apply
   automatically. Rerun it after adding a portal admin to provision that person's
   Portainer account. Removing Admin blocks public access within 15 seconds;
   the pipeline does not delete the person's Portainer account or private-network
   credentials. Direct NodePort access retains Portainer's own authentication.
6. Verify with an actual Cloudflare admin sign-in and a non-admin sign-in before
   considering SSO operational. Check the callback, Portainer environment access,
   and denial after unchecking Admin. Automated tests use mocked provider APIs;
   they do not substitute for this first real browser exchange.

The CI Kubernetes identity needs get/create/patch on the SSO Secrets, get on the
bootstrap Secret in `portainer`, and get/exec on the portal deployment/pods.
The portal must already have migration `0005_application_admin_only` applied.
The adapter is executed transiently inside the existing portal pod; no image
rebuild is required to run it, and the Cloudflare token never enters that pod.

## Adding an app

Add a catalog entry with a unique `id`, name, exact hostname, authentication
mode, and explicit `admin_only` value. OIDC apps also need exact HTTPS
`redirect_uris`; only hostnames directly under the existing wildcard are allowed.
For a generic OIDC app, mount/read its generated Secret in the application and
configure the application's native OIDC settings. Further app-specific adapters
can automate that final configuration step. Portainer is the first adapter.

```powershell
.\.venv\Scripts\python.exe sso/provision.py
.\.venv\Scripts\python.exe -m unittest discover -s sso/tests -v
```

The default command validates the catalog locally without credentials, network
calls or changes. `--apply` performs reconciliation and requires `kubectl`,
`requests`, cluster access, and the two Cloudflare environment variables above.

## State and recovery

Cloudflare returns an OIDC client secret only at creation. The first apply saves
it immediately in Kubernetes, before configuring the application. Later applies
reuse the same client and secret; they never rotate it automatically. Back up
`fleet-sso/fleet-sso-*` through your normal encrypted Kubernetes backup process.
If a matching Cloudflare app exists but its Secret is missing, provisioning stops
instead of making duplicates or silently invalidating credentials. Restore the
Secret, or deliberately recreate the client after reviewing the impact. An
interrupted initial creation can require this manual recovery too.

Server-side Secret apply avoids a plaintext last-applied annotation. Credentials
travel via stdin to Kubernetes/portal processes; API bodies, subprocess output,
and remote tracebacks are suppressed on failure. No credentials are emitted as
Actions outputs or artifacts. Kubernetes Secrets still require the cluster's
normal RBAC, encryption-at-rest and backup protections.

The initial Portainer admin is retained for recovery. Use
`https://portainer.example.com/#!/internal-auth` with the private initial login
if OAuth needs repair. To roll back the application authentication method, use
that account to select Internal authentication; keep the portal's Admin-only
restriction and the existing Cloudflare gate enabled.

References: [Cloudflare OIDC setup](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/saas-apps/generic-oidc-saas/),
[Portainer OAuth configuration](https://docs.portainer.io/admin/settings/authentication/oauth).
