#!/usr/bin/env bash
# Joins this node (homelab Ubuntu box or Oracle ARM cloud box) to the ArcoreFleet tailnet.
#
# Usage:
#   TS_AUTHKEY=tskey-auth-xxxxxxxxxxxx ./join-tailscale.sh <hostname> [tags]
#
# Examples:
#   TS_AUTHKEY=tskey-... ./join-tailscale.sh homelab-primary   tag:homelab
#   TS_AUTHKEY=tskey-... ./join-tailscale.sh homelab-secondary tag:homelab
#   TS_AUTHKEY=tskey-... ./join-tailscale.sh oracle-arm-1      tag:cloud
#
# Generate TS_AUTHKEY in the Tailscale admin console:
#   https://login.tailscale.com/admin/settings/keys
# Use a reusable, non-ephemeral key tagged with the ACL tags you intend to pass below
# (ACL must pre-authorize the tag; see tailscale-acl.json in this directory).
set -euo pipefail

: "${TS_AUTHKEY:?Set TS_AUTHKEY to a Tailscale auth key before running this script}"
NODE_HOSTNAME="${1:?Usage: $0 <hostname> [tags]}"
TAGS="${2:-tag:arcorefleet}"

if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

sudo tailscale up \
  --authkey="${TS_AUTHKEY}" \
  --hostname="${NODE_HOSTNAME}" \
  --advertise-tags="${TAGS}" \
  --accept-dns=true \
  --ssh

echo "Joined tailnet as ${NODE_HOSTNAME} (${TAGS})."
echo "Tailscale IP: $(tailscale ip -4)"
