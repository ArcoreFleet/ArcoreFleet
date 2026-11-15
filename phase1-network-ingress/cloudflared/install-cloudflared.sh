#!/usr/bin/env bash
# Installs cloudflared and creates/binds the ArcoreFleet tunnel.
# Run on the same host as the Traefik container (phase1-network-ingress/traefik).
#
# `cloudflared tunnel login` opens a browser-based auth flow against your
# Cloudflare account and cannot be scripted non-interactively, so steps 2-3
# must be run by hand the first time.
set -euo pipefail

TUNNEL_NAME="arcorefleet-ingress"
DOMAIN_WILDCARD="*.example.com"

echo "== 1/5: installing cloudflared =="
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update
sudo apt-get install -y cloudflared

echo "== 2/5: authenticate with Cloudflare (opens a browser link) =="
cloudflared tunnel login

echo "== 3/5: create tunnel '${TUNNEL_NAME}' (skips if it already exists) =="
cloudflared tunnel create "${TUNNEL_NAME}" || true
TUNNEL_ID=$(cloudflared tunnel list --output json | python3 -c "import json,sys; print([t['id'] for t in json.load(sys.stdin) if t['name']=='${TUNNEL_NAME}'][0])")

echo "== 4/5: route DNS for ${DOMAIN_WILDCARD} -> tunnel =="
cloudflared tunnel route dns "${TUNNEL_NAME}" "${DOMAIN_WILDCARD}"

echo "== 5/5: install credentials + config, register as systemd service =="
sudo mkdir -p /etc/cloudflared
sudo cp "${HOME}/.cloudflared/${TUNNEL_ID}.json" "/etc/cloudflared/${TUNNEL_ID}.json"
sed "s/__TUNNEL_ID__/${TUNNEL_ID}/" "$(dirname "$0")/config.yml" | sudo tee /etc/cloudflared/config.yml >/dev/null
sudo cloudflared service install
sudo systemctl enable --now cloudflared

echo "Tunnel '${TUNNEL_NAME}' (${TUNNEL_ID}) is live, routing ${DOMAIN_WILDCARD} -> http://localhost:8880"
