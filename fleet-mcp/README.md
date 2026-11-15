# Fleet MCP gateway - one API key for every fleet app

**http://fleet-control.example-tailnet.ts.net:8880/mcp**, on the tailnet only - an
[MCP](https://modelcontextprotocol.io) server (streamable HTTP) that lets Claude Code, other agents and scripts read
and write the ArcoreFleet apps with a single **portal API key**. The key acts
as its owner: the same apps the portal lets them open, the same permissions
inside each app.

## Using it

1. Install Tailscale on the machine and sign in to the Arcore tailnet. The
   gateway is not reachable from the internet.
2. In the portal, open **API keys** and create a key. It is shown once.
3. Add it to Claude Code:

   ```bash
   claude mcp add --transport http arcorefleet http://fleet-control.example-tailnet.ts.net:8880/mcp \
     --header "Authorization: Bearer afk_..."
   ```

Tools:

| Tool | What it does |
|---|---|
| `list_apps` | Apps this key can use, with each app's API conventions |
| `list_endpoints(app, search?)` | Operations from the app's OpenAPI schema |
| `describe_endpoint(app, method, path)` | Parameters and request body schema of one operation |
| `call_api(app, method, path, query?, body?)` | Calls the app as the key's owner: GET, POST, PUT, PATCH, DELETE |

Writes take effect immediately; there is no dry run.

## How it works

```
client on the tailnet ── Bearer afk_… ──► WireGuard ──► Traefik :8880 on fleet-control ──► fleet-mcp
                         (fleet-mcp-tailnet: source must be 100.64.0.0/10; Host must be the tailnet name)
                                                                                    │
     1. POST portal /api/keys/resolve (in-cluster, shared secret) → owner email + permitted apps
     2. app not permitted → refused here, the app is never called
     3. call the app's in-cluster Service with X-Arcore-Fleet-Token:
        RS256, 60 s, email = the owner, aud = the app's hostname, iss = https://mcp.example.com
                                                                                    ▼
                                     arcorethread / arcoreestate-backend (verify via /.well-known/jwks.json)
```

- **Access rules are the portal's.** Key resolution uses the same `can_access`
  as forwardAuth, and is cached 15 seconds like forwardAuth, so revoking a
  key, deactivating a user or removing an app grant applies within 15 seconds.
- **The API key never reaches an app.** Apps get only the fleet token, valid
  for 60 seconds and for one app (the audience is that app's hostname).
- **Fails closed.** No key, an unknown key, or a portal that can't be reached
  means no call. Without `FLEET_MCP_SHARED_SECRET` and `FLEET_SIGNING_KEY` the
  gateway does not start.
- **Audit log.** Every call is logged with the owner, key name, app, method,
  path, status and time: `kubectl -n apps logs deploy/fleet-mcp`.
- **Stateless.** No sessions; any replica can answer any request.
- **Tailnet only, two checks.** The Ingress matches only fleet-control's tailnet
  name, which Cloudflare never routes into the tunnel, and its middleware
  (`phase1-network-ingress/traefik/dynamic/fleet-mcp.yml`) admits only tailnet
  source addresses - tunnel traffic arrives from 127.0.0.1 and is refused even
  with a forged Host header. The gateway itself answers only that Host name.
  Plain HTTP is fine here: Tailscale encrypts the connection end to end, and
  Cloudflare is not on the path, so no third party sees the key.

## Adding an app

1. The app accepts `X-Arcore-Fleet-Token` (verify RS256 against
   `FLEET_JWKS_URL`, issuer `FLEET_TOKEN_ISSUER`, audience = its own hostname)
   and resolves the email to a user exactly as it does for Cloudflare Access.
   Reference implementations: ArcoreThread `backend/app/core/identity.py`
   (FastAPI), ArcoreEstate `backend/core/fleet_auth.py` (Django REST Framework).
2. Its manifest sets:

   ```yaml
   - name: FLEET_TOKEN_ISSUER
     value: "https://mcp.example.com"
   - name: FLEET_TOKEN_AUDIENCE
     value: "<its hostname>"
   - name: FLEET_JWKS_URL
     value: "http://fleet-mcp.apps.svc.cluster.local/.well-known/jwks.json"
   ```

3. Add it to `fleet_mcp/apps.json`: its in-cluster Service URL, the path of
   its OpenAPI schema, and notes for the model. Merging redeploys the gateway.

The app must also be registered in the portal (Applications); the portal
decides who may use it.

## Deploy (once)

```bash
# 1. Shared secret between portal and gateway, and the gateway's signing key.
SECRET=$(openssl rand -hex 32)
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out fleet-signing.pem
kubectl -n apps create secret generic fleet-mcp-env \
  --from-literal=FLEET_MCP_SHARED_SECRET="$SECRET" --from-file=FLEET_SIGNING_KEY=fleet-signing.pem
kubectl -n apps patch secret portal-env -p "{\"stringData\":{\"FLEET_MCP_SHARED_SECRET\":\"$SECRET\"}}"
shred -u fleet-signing.pem

# 2. The tailnet-only middleware, on fleet-control (Traefik watches the folder; no restart).
cp ../phase1-network-ingress/traefik/dynamic/fleet-mcp.yml ~/arcorefleet/traefik/dynamic/

# 3. The gateway (CI then updates the image on every merge touching fleet-mcp/).
kubectl apply -f k3s/fleet-mcp.yaml
kubectl -n apps rollout restart deployment/portal   # picks up FLEET_MCP_SHARED_SECRET
```

DNS, the tunnel and Cloudflare Access need nothing: the gateway has no public hostname.

**Rotating the signing key:** replace `FLEET_SIGNING_KEY` and restart the
gateway. Apps refetch the JWKS when a token names a key they haven't seen.
Tokens live 60 seconds, so there is no overlap to manage.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q    # a real MCP client against the server, with the portal and apps mocked
```

## Limits

- Only devices on the tailnet can use it, so claude.ai connectors and cloud
  agents cannot. That is deliberate (product owner, 2026-10-24).
- `call_api` returns at most 100,000 characters of a response body.
- Redirects are reported, not followed (Django apps need the trailing slash).
