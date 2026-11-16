#!/usr/bin/env bash
# Tells this node's K3s containerd to trust the private registry over
# plain HTTP (no TLS cert exists for an internal-only registry reachable
# solely over the tailnet). Run on every K3s node - server and agents.
#
# Usage: ./configure-node.sh <registry-tailscale-ip>:30500
set -euo pipefail

REGISTRY="${1:?Usage: $0 <registry-tailscale-ip>:30500}"

# Do not erase authentication (or unrelated registries) during reconfiguration.
if sudo test -e /etc/rancher/k3s/registries.yaml; then
  echo "registries.yaml already exists; merge changes securely as described in AUTH.md." >&2
  exit 1
fi

sudo mkdir -p /etc/rancher/k3s
sudo tee /etc/rancher/k3s/registries.yaml > /dev/null <<EOF
mirrors:
  "${REGISTRY}":
    endpoint:
      - "http://${REGISTRY}"
EOF

echo "Before pulling, merge the node credentials described in AUTH.md."

if systemctl is-active --quiet k3s; then
  sudo systemctl restart k3s
elif systemctl is-active --quiet k3s-agent; then
  sudo systemctl restart k3s-agent
else
  echo "No active k3s or k3s-agent service found on this node." >&2
  exit 1
fi

echo "Configured ${REGISTRY} as a trusted insecure registry on this node."
