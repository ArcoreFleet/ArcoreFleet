#!/usr/bin/env bash
# Run on the K3s server node, after applying rbac.yaml. Builds a
# self-contained kubeconfig scoped to the ci-deployer ServiceAccount and
# prints it base64-encoded, ready to paste into a GitHub Actions secret
# (KUBE_CONFIG_CI).
set -euo pipefail

SERVER_TAILSCALE_IP="${1:?Usage: $0 <server-tailscale-ip>}"

TOKEN=$(sudo k3s kubectl get secret ci-deployer-token -n apps -o jsonpath='{.data.token}' | base64 -d)
CA=$(sudo cat /etc/rancher/k3s/k3s.yaml | grep 'certificate-authority-data' | awk '{print $2}')

cat > /tmp/ci-kubeconfig.yaml <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: arcorefleet
    cluster:
      server: https://${SERVER_TAILSCALE_IP}:6443
      certificate-authority-data: ${CA}
contexts:
  - name: ci-deployer
    context:
      cluster: arcorefleet
      namespace: apps
      user: ci-deployer
current-context: ci-deployer
users:
  - name: ci-deployer
    user:
      token: ${TOKEN}
EOF

base64 -w0 /tmp/ci-kubeconfig.yaml
echo
shred -u /tmp/ci-kubeconfig.yaml 2>/dev/null || rm -f /tmp/ci-kubeconfig.yaml

echo "Paste the base64 blob above into a GitHub secret named KUBE_CONFIG_CI." >&2
