# Cloudflare Access — Google/Microsoft email gate for `*.example.com`

Gates every subdomain behind the Cloudflare Tunnel (Phase 1) so visitors must
sign in with an allowed Google or Microsoft account before Traefik ever sees
the request. Terraform-managed; the OAuth app registrations in Google/Azure
are one-time manual steps (both require clicking through a console UI).

## Prerequisites (manual, one-time)

1. **Enable Cloudflare Zero Trust** on the account if not already active:
   dashboard -> Zero Trust -> pick a team name, e.g. `arecorellc`. This fixes
   your Access callback URL:
   `https://example.cloudflareaccess.com/cdn-cgi/access/callback`
2. **Google OAuth client** (console.cloud.google.com):
   - APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
   - Application type: Web application
   - Authorized redirect URI: the callback URL from step 1
   - Copy the generated Client ID + Client Secret
3. **Microsoft (Azure AD) app registration** (portal.azure.com):
   - Azure Active Directory -> App registrations -> New registration
   - Redirect URI (Web): same callback URL from step 1
   - API permissions: add `email`, `openid`, `profile` (Microsoft Graph,
     delegated)
   - Certificates & secrets -> New client secret -> copy the secret **value**
     (only shown once)
   - Copy the Application (client) ID and Directory (tenant) ID from the
     Overview page
4. **Cloudflare API token**: dashboard -> My Profile -> API Tokens -> Create
   Token -> Custom token -> add permissions **Account > Access: Apps and
   Policies > Edit** and **Account > Access: Organizations, Identity
   Providers, and Groups > Edit**. Export it:
   ```bash
   export CLOUDFLARE_API_TOKEN=cf-token-here
   ```

## Apply

```bash
cp terraform.tfvars.example terraform.tfvars
# fill in terraform.tfvars with the values collected above
# (terraform.tfvars is gitignored — never commit it)

terraform init
terraform plan
terraform apply
```

This creates:
- Two Access identity providers: Google, Microsoft (azureAD)
- One Access application covering `*.example.com`
- One allow policy admitting anyone whose email is in `allowed_emails`, or
  whose email domain is in `allowed_email_domains` (e.g. all
  `@example.com` Microsoft 365 accounts)

Anyone not matching the policy sees Cloudflare's hosted login page and is
denied after authenticating — they never reach Traefik or the app.

**Status: applied and verified.** `testapp.example.com` redirects to
the Access login as expected; `www`/`sandbox` confirmed unaffected after
fixing the proxy-status drift described below.

## Notes

- Add/remove allowed emails or domains by editing `terraform.tfvars` and
  re-running `terraform apply` — no changes needed on the Traefik/K3s side.
- **Critical**: `www` and `sandbox` must stay set to **DNS only** (grey
  cloud) in Cloudflare DNS. Access only intercepts traffic that's proxied
  through Cloudflare's edge — if either of those is ever switched to
  Proxied, it would suddenly fall under this `*.example.com` wildcard
  and get gated behind the Google/Microsoft login too.
- To exempt a specific subdomain from the gate later (e.g. a public landing
  page), create a separate `cloudflare_zero_trust_access_application` scoped
  to just that hostname with a `decision = "bypass"` policy — more specific
  hostnames take precedence over the wildcard.
