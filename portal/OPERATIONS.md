# Fleet operations

The portal home screen provides **Fleet dashboard** and **Fleet settings** tiles
to active portal admins, with matching navigation links. The dashboard at
`/operations/` combines
dated observations of GitHub Actions, K3s Deployments, BrowserForge test runs, and
server CPU/memory/disk performance. Page requests read Postgres snapshots. The
admin-only **Poll now** POST queues a request; collection runs in a separate worker.

**Fleet settings** at `/settings/` manages the 5- or 10-minute polling interval,
monitored server endpoints, and links to application/environment and user access
configuration. Dashboard and settings routes enforce staff access independently
of whether their links are visible.

GitHub Actions remains responsible for builds and deployment controls. K3s remains
responsible for rollout and workload lifecycle. BrowserForge remains responsible
for test execution. This adds no pipeline executor, broker, or credential database.

## Configure applications and environments

1. In **Applications**, set each application's repository as `owner/repository`.
2. Edit the application and add an **Observed environments** row. Save, then edit
   again to add another row. Each row has an environment name, GitHub branch,
   optional workflow filename (for example `build-and-deploy.yml`), Kubernetes
   namespace and Deployment, and optional container name. Multi-container
   Deployments require an explicit container name.
3. Set the public URL for environments on a different hostname. It defaults to
   the application's URL and is used to match BrowserForge's `baseUrl`.

The page has no deployment trigger, retry, cancel, rollout, or secret-management controls.
Environment edits and repository edits clear affected observations. Removing an
environment deletes only its observation configuration and snapshots.

## Collect observations

After migrations have run, execute from `portal/`:

```sh
python manage.py sync_operations
```

For periodic collection and the **Poll now** button, run one persistent collector:

```sh
python manage.py sync_operations --loop
```

For the verified production endpoints, register the reviewed file once:

```sh
python manage.py configure_operations_servers host-metrics/servers.json
```

The optional collector Deployment does this automatically at startup through
`--server-config`, using the `fleet-operations-servers` ConfigMap mounted read-only.
The command upserts servers by name, retains snapshots when configuration is
unchanged, and never deletes unlisted servers. Create/update that ConfigMap from
the reviewed JSON before starting the collector; it contains no credentials.

The default automatic interval is **10 minutes**. Admins can choose **5 or 10
minutes** on the page; other values are rejected. The collector checks a small
database control row every five seconds for manual requests. These idle checks do
not contact metrics services or other upstream APIs. The button requests all
servers/application environments, or just the selected server. It coalesces
requests while one is queued/running, with a one-minute manual cooldown and a
database lease to prevent overlapping collection. A selected-server refresh does
not postpone the automatic all-server sweep. The page reports collector heartbeat,
queued/running status and source failures; reload to see completed observations.

| Environment variable | Purpose |
| --- | --- |
| `FLEET_GITHUB_TOKEN` | Optional fine-grained GitHub token with Actions read access to the selected repositories. Needed for private repositories. |
| `FLEET_BROWSERFORGE_URL` | Optional internal BrowserForge API URL, such as `http://browserforge:8434`. |
| `FLEET_KUBERNETES_ENABLED` | Set to `true` for in-cluster Deployment observation. Disabled by default. |
| `FLEET_K8S_TOKEN_FILE` | Mounted service-account token; defaults to the standard Kubernetes location. |
| `FLEET_K8S_CA_FILE` | Mounted Kubernetes CA; defaults to the standard location. TLS verification remains enabled. |

Supply credentials using existing Kubernetes Secrets. They are never editable in
the UI or stored in snapshots. The collector stores selected status fields only;
it does not store BrowserForge environment variables, raw upstream payloads, logs,
or Kubernetes pod environment configuration. HTTP redirects are refused.

Adapt the optional collector Deployment in `k3s/operations-collector.yaml`. Set its image to the same immutable
portal image as the deployment. Its service account has only `get` permission on
Deployments in `apps`. Add a similarly scoped RoleBinding in any additional
namespace that needs observation. The web portal needs no Kubernetes permission.

The collector requires the portal migrations and existing `portal-db` and
`portal-env` Secrets. Apply the optional manifest only after the portal rollout
has applied the new database migration. The example is not applied by CI.

If an earlier collector CronJob was installed, remove or suspend it when switching
to the Deployment; do not run an independent forced-poll scheduler alongside the
interval-aware worker. The one-shot command is retained for troubleshooting.

## Oracle and homelab server performance

In **Operations → Add server**, register each host independently: a display name,
its private/Tailscale `node_exporter` metrics URL, and comma-separated absolute
mountpoints (default `/`). Examples are `http://100.x.y.z:9100/metrics` for the
Oracle host and a different tailnet IP for the homelab. Use **All servers** or
select one host. Hosts need not be Kubernetes nodes. Disable a server to skip its
automatic and manual collection while retaining the last snapshot.

The collector's node/pod must have a working route and Tailscale ACL permission to
each metrics endpoint. A working tailnet on an administrator's laptop alone does
not establish connectivity from the collector pod. Check from the collector's
network context. Do not publish metrics through a public Ingress or Tunnel.

Use an existing host node_exporter, or install the official Linux binary for the
server architecture and adapt [the systemd example](node-exporter.service.example).
Set its bind address to that host's Tailscale IP and allow TCP 9100 only from the
collector's tailnet identity/node. The example enables only CPU, memory, root
filesystem and host identity collectors. Add every additional disk mount to its
filesystem include expression as well as the portal's mountpoint list.

Run the exporter on the **host**. A container's filesystem/memory values are not
equivalent to server performance. Containerized exporters require the host
namespace/filesystem setup in the [official node_exporter instructions](https://github.com/prometheus/node_exporter#docker).
This feature does not require a Prometheus database, Kubernetes node-proxy access,
SSH execution, host mounts in the portal, or OCI API credentials.

Snapshots contain:

- CPU utilization from two counter scrapes separated by at least one second,
  logical CPU count and separate CPU steal percentage. Guest counters are excluded
  from the total to avoid double counting; iowait and steal are excluded from busy.
- Memory total, used and available; used is total minus `MemAvailable`, so
  reclaimable cache is not counted as unavailable memory.
- Total, used and space available to an unprivileged process for each configured
  host filesystem. Disk percentage uses `used / (used + available)`, like `df`,
  accounting for reserved blocks. Missing or ambiguous mountpoints are errors.
- Warning labels at 80% and critical labels at 90% utilization. These are simple
  snapshot thresholds, not paging alerts. CPU is a short sample, not a historical
  average or a capacity guarantee.

Only the latest snapshot per host is retained; there is no time-series growth.
Unavailable servers retain dated last-known data with an error. Each poll does
two bounded HTTP scrapes per enabled host; responses are capped at 2 MiB. No host
credentials are accepted in URLs. The optional collector is one idle Python
process with a 128 MiB memory/250m CPU limit, rather than a fresh scheduled pod for
every poll. Exporters add their own small resident process on each host; measure
the actual overhead after enabling them on the Oracle instance.

## Existing Oracle database collector

The Oracle host already runs `srv-metrics.service` / `srv-metrics.timer`, an
hourly Python collector in `/home/ubuntu/Projects/OracleDBTest/collect.py`. It
produces `/home/ubuntu/Projects/Tools/Monitor/DB_SUMMARY.md`, including storage
metrics for the Oracle database `arcoredb01`.

The separate `fleet-oracle-report.timer` reads that cached report every five
minutes and atomically publishes a small node_exporter textfile. It does not
connect to Oracle, copy wallet credentials, change the existing service, or
increase the database query frequency. Only report availability, original
collection timestamp, total allocated storage and tablespace utilization are
exported; schema/table inventories and installed-application details are excluded.

Enable **Database metrics** for the Oracle server. Fleet then shows those storage
values and the original source timestamp alongside host metrics. Because the
source is hourly, its stale threshold is two hours, independent of the host's
15-minute snapshot threshold. A report-adapter failure has an explicit database
error and does not hide otherwise valid CPU/memory/disk observations.

See [host-metrics deployment](host-metrics/README.md) for the configured endpoints,
reproducible installation and activation of the endpoint ConfigMap after merge.

## Interpret results

- An environment not checked in 15 minutes is marked **stale**. Every source also
  shows its own last successful observation time.
- A failed source retains dated last-known data and displays an error. Other
  sources still refresh. The command exits nonzero if a configured source fails.
- Unconfigured sources are identified explicitly; they are not treated as passing.
- A workflow result is the latest run on the configured branch, optionally
  restricted to a specific workflow. It is not a claim that the deployed image
  passed that workflow.
- Rollout readiness requires the observed generation, updated replicas, available
  replicas, total replicas and ready replicas to match. Scaled-to-zero and failed
  rollouts have distinct labels. The displayed image comes from the Deployment's
  desired pod template, not a running pod's image digest.
- An image tag is shown as a commit only when it is 7–40 hexadecimal characters.
  Other tags and digests show an unknown commit.
- BrowserForge currently exposes the latest 100 runs globally. The collector
  matches repository and environment URL, then selects the latest matching run.
  Older runs outside that window are reported as absent, not successful. Its
  revision is displayed separately from the deployed image commit.

## Validation and deployment

Local validation covers Django checks, migration-drift checks, and the portal
tests. Adding these as a required CI job before production deployment is a
separate follow-up that needs GitHub workflow-write permission. The existing
portal build/deploy workflow still runs on merge. This PR adds no production
credentials or automatic collector deployment. Back up the portal database using
the existing procedure before applying the additive migration in production.
