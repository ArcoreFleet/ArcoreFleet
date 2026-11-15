# Private host endpoints

Configured and verified on 2026-10-20:

| Host | Endpoint | Metrics |
| --- | --- | --- |
| Oracle `fleet-control` | `http://192.0.2.10:9100/metrics` | Host CPU/memory/root disk + cached Oracle database report |
| Homelab `fleet-worker` | `http://192.0.2.11:9100/metrics` | Host CPU/memory/root disk |

Both return HTTP 200 from the production portal pod, with the expected host name
and exactly one root-filesystem sample. Listeners bind only their respective
Tailscale addresses. Nothing is published through public Ingress/Cloudflare.

## Reproduce endpoint setup

Copy this directory to each host, then run `sh install.sh <that-host-tailnet-IP>`
as the existing Docker operator. It verifies the bind IP belongs to the host,
pulls node_exporter v1.12.1, resolves and pins its immutable image digest, and
starts a `fleet-node-exporter` container with `unless-stopped` restart policy.
An existing container with the same name is changed only if its Fleet ownership
label matches. Deployment files live under the operator's
`~/.local/share/fleet-metrics/deployment/` directory.

The deployed digest on both architectures is:
`quay.io/prometheus/node-exporter@sha256:1b4e4438faca4dd7e001dd445d161a4a2091b0fededa84093b3a8dfeae1f1be0`.

The container follows node_exporter's host-monitoring setup: host network/PID
namespace and a read-only host-root mount, with a non-root process, no Linux
capabilities, a read-only container filesystem, no privilege escalation, and
64 MiB / 0.10 CPU limits. Only CPU, memory, root filesystem, host identity and
textfile collectors are enabled. It listens on tailnet TCP 9100 only. The existing
Fleet-node network permits the cloud/homelab mesh. This setup makes no ACL changes.

Additional mountpoints must be included in the exporter's filesystem regex and
the portal server record. The example exports `/`, which holds the relevant
application/data storage on both configured hosts.

## Reuse the Oracle report

On Oracle only, run `sh install-oracle-adapter.sh` as `ubuntu` with its existing
noninteractive sudo rights. It installs the cached-report adapter and a five-minute
systemd timer. It does not modify or restart `srv-metrics.service` / its hourly
timer. The adapter's output is
`~/.local/share/fleet-metrics/textfile/oracle_db.prom`, read-only in the exporter.
The adapter uses about one short Python invocation per five minutes, with 64 MiB
memory and 10% CPU limits. No new database credentials or queries are introduced.

## Activate portal observations after merge

The live portal remains on its existing production image until PR #1 is merged
and deployed. Its current image does not contain these models or collector.
The endpoint configuration has already been stored in `apps/fleet-operations-servers`
as a ConfigMap; it has no effect on the old portal image.

After the portal's migrations have run, pin `../k3s/operations-collector.yaml` to
the new portal image and apply it. Its startup command reads the mounted endpoint
JSON and registers both servers, then honors the default 10-minute interval and
manual requests. For a different cluster, create the ConfigMap first:

```sh
kubectl -n apps create configmap fleet-operations-servers \
  --from-file=servers.json=portal/host-metrics/servers.json \
  --dry-run=client -o yaml | kubectl apply -f -
```

This command is run from the repository root. No collector Deployment has been
installed into production yet because it requires the PR's image and migrations.

## Validation and overhead

A live snapshot was successfully parsed for both hosts and all six Oracle
tablespaces. During an idle Docker stats sample, exporter memory was about
6.2 MiB on Oracle and 6.8 MiB on the homelab, with CPU reported at 0.00% at that
instant. These are point-in-time measurements, not reserved resource usage or
load guarantees. Host snapshots remain short samples; database values retain the
hourly report's actual collection time.
