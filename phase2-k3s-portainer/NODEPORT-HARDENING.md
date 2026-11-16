# Restrict NodePort interfaces

The worker's LAN address currently accepts TCP connections on 30480 (portal),
30443 (authority), and 30500 (registry). This bypasses the assumption that
NodePorts are reachable only through Tailscale. The proposed drop-in restricts
NodePorts on every node to IPv4 loopback and Tailscale addresses.

## Before changing a running node

- Confirm you are connected to the intended Fleet cluster. A local kubeconfig
  can point at an unrelated cluster; compare node names and addresses first.
- Review `/etc/rancher/k3s/config.yaml`, alphabetical `config.yaml.d/*.yaml`,
  service arguments and environment for existing `kube-proxy-arg`, proxy mode,
  custom config paths or command-line overrides. Do not print files containing
  tokens. Consolidate conflicting nodeport-addresses settings before rollout.
- This configuration assumes IPv4 and iptables kube-proxy. Existing loopback
  NodePorts require that mode and must not have iptables-localhost-nodeports
  disabled. Do not use this drop-in unchanged for an IPv6/nftables deployment.
- Inventory ALL NodePorts, including Portainer and app endpoints. Any intentional
  LAN access must move to a reviewed authenticated route before this change.
- Keep a second SSH session open. Restart one node at a time. The single
  control-plane restart temporarily interrupts API access.

## Rollout

Copy `90-nodeport-addresses.yaml` to a temporary path on the worker first.
Check that the destination file does not already exist; preserve any existing
file through the site's configuration-management process instead of overwriting.

```bash
sudo test ! -e /etc/rancher/k3s/config.yaml.d/90-nodeport-addresses.yaml
sudo install -d -m 0755 /etc/rancher/k3s/config.yaml.d
sudo install -m 0644 90-nodeport-addresses.yaml /etc/rancher/k3s/config.yaml.d/90-nodeport-addresses.yaml
sudo systemctl restart k3s-agent
sudo systemctl is-active k3s-agent
```

From the control plane, wait for the worker to be Ready. Verify fresh registry
pulls and Tailscale NodePort access; from the LAN, verify TCP connections to the
worker's LAN address fail. Then repeat on the control plane using `k3s` rather
than `k3s-agent`. Verify loopback `/healthz` on port 30480 still works, public
portal SSO still works, the authority's pinned TLS endpoint still works, and
registry pushes/pulls still work. Confirm all nodes return Ready.

Use an independent external network to check public-IP reachability. A timeout
is an observation, not proof of a permanent firewall policy. Keep host/cloud
firewall restrictions and Tailscale access policies as separate controls.

## Rollback

If a node fails readiness or an intended route breaks, remove only the new
`/etc/rancher/k3s/config.yaml.d/90-nodeport-addresses.yaml` installed in this
change and restart that node's service. Recheck readiness before doing anything
to the other node. Do not clear the base config, other drop-ins, or firewall rules.

This closes LAN/public *destination interfaces* for NodePorts. It does not grant
least-privilege access among tailnet clients, secure host-networked port 8880,
encrypt registry HTTP, or isolate ClusterIP traffic from compromised pods.
Those remain separate controls in the security checklist.

References: [K3s drop-ins and append semantics](https://docs.k3s.io/installation/configuration),
[Kubernetes NodePort address filtering](https://v1-36.docs.kubernetes.io/docs/concepts/services-networking/service/).
