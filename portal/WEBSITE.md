# Website manager

Admin-only portal pages for running **example.com** (`example-owner/arcore-website`):
review pull requests, blog posts and catalog changes, then publish them. Open it
from the **Website** tile or nav link (`/website/`).

## How changes reach the live site

The manager never edits the live site. Every change is a pull request on the website
repository, and the website's existing pipeline stays in charge:

```
portal page ──► website worker ──► branch + pull request on arcore-website
                                        │
                                        ├─ CI "quality": lint, catalog and blog checks, build, e2e
                                        ├─ Vercel preview deployment
                                        ▼
                             merge to main ──► Vercel production deploy
```

The portal's web pods only read the stored snapshot and record requests in the
database. The **website worker** (`k3s/website-worker.yaml`) is a separate
Deployment running the portal image with `sync_website --loop`, and it is the only
pod that holds the GitHub credential. This mirrors the operations collector.

## What step 1 does

The review queue is read-only:

- **Open pull requests**: CI state (and the `quality` job), merge state, the Vercel
  preview link, what kind of files change (blog, catalog, content, code), and for
  blog posts their release number, draft state and remaining `TODO(` placeholders.
- **Draft posts** and **published posts** on `main`, and the next free release number.
- **Refresh now** queues a refresh; otherwise the worker refreshes every
  `WEBSITE_SYNC_MINUTES` (default 10).

The worker asks GitHub for installation tokens limited to read permissions, even
though the app will later be granted write access for approving and publishing.

Later steps add Approve and Publish (merge only when checks pass), blog drafting and
editing, catalog entries, and edits to content files under `content/` and `lib/data/`.

## One-time setup

### 1. Create the GitHub App

GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**:

- **Name**: e.g. `arcore-website-manager`; **Homepage**: `https://portal.example.com`.
- **Webhook**: uncheck **Active** (the worker polls; nothing calls in).
- **Repository permissions**:
  - Contents: **Read and write**
  - Pull requests: **Read and write**
  - Checks: **Read-only**
  - Deployments: **Read-only**
  - Metadata: **Read-only** (required)
- **Where can this GitHub App be installed?**: Only on this account.

Create it, note the **App ID**, and **Generate a private key** (a `.pem` download).
Then **Install App** → only select repository `arcore-website`, and note the
installation ID from the URL (`.../settings/installations/<installation-id>`).

Write access is granted now so later steps need no reinstall; step 1 never uses it.

### 2. Store the credential

```bash
kubectl -n apps create secret generic portal-website-github \
  --from-literal=app-id=<app-id> \
  --from-literal=installation-id=<installation-id> \
  --from-file=private-key.pem=<path-to-downloaded.pem>
```

Delete the downloaded `.pem` afterwards. To rotate, generate a new key in the app
settings, replace the secret, restart the worker, then delete the old key in GitHub.

### 3. Install the worker

After a portal build has run the migrations (CI deploys the portal on merge):

```bash
IMAGE=$(kubectl -n apps get deploy portal -o jsonpath='{.spec.template.spec.containers[0].image}')
sed "s|REPLACE_WITH_PORTAL_IMAGE_AT_COMMIT_SHA|$IMAGE|" k3s/website-worker.yaml | kubectl apply -f -
kubectl -n apps logs deploy/portal-website
```

Install the worker once after configuring its Secret. Every portal build (`docs/examples/workflows/portal.yml`) tests the portal and website routes, then updates an installed worker to the portal image after migrations. A missing worker produces a deployment warning. CI cannot create Deployments or read Secrets; restore a missing worker with the setup command above. Without its credential, automatic refresh is not configured.
The page shows **Website worker online** once it reports, and the first snapshot
within a few seconds.

### 4. Protect the website's main branch (recommended before Publish exists)

On `arcore-website` → Settings → Branches, add a rule for `main` that requires the
**quality** status check. Then nothing, including this app, can merge a pull request
whose CI is red.

## Troubleshooting

| Page says | Meaning |
|---|---|
| Website worker not reporting | The `portal-website` Deployment is missing, crashing, or cannot reach Postgres. Check its logs. |
| GitHub App private key not mounted | The `portal-website-github` secret or its `private-key.pem` key is missing. |
| Refresh failed; check the GitHub App installation… | GitHub refused or failed a call: the app is not installed on the repository, lacks a permission above, or the IDs are wrong. The last good snapshot stays visible with its age. |
| No preview deployment reported | Vercel has not reported a successful preview deployment for the PR's latest commit yet. |

## Recovery from the November 7 UI rollout

The shared Fleet UI was deployed from a branch that predated the website-manager merge. That image omitted the website app, URL route, home tile, and navigation link. The cluster also lacked both the website worker and its GitHub App Secret. Restore the website app together with the Fleet sidebar/theme; do not deploy an older branch to recover only the link.

The navigation tests cover both the Website links and the shared Fleet shell. CI now runs the portal and website tests before building and reports a visible warning if the worker is missing. Build portal images from a branch containing the current main branch and keep the deployable UI changes merged there. A missing credential must be configured separately; it cannot be recovered from Git history.
