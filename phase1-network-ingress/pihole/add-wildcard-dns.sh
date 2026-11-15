#!/usr/bin/env bash
# Adds a local DNS override on Pi-hole for one app hostname under
# example.com, so clients inside the network resolve it directly to the
# Traefik ingress node's Tailscale IP instead of round-tripping out to
# Cloudflare and back in.
#
# This is intentionally per-app, NOT a domain-wide wildcard: app hostnames
# live directly under example.com (e.g. myapp.example.com), the same
# level as the existing www/sandbox records. A wildcard override for all of
# example.com would hijack internal lookups for those too.
#
# Run on the Pi-hole host.
# Usage: ./add-wildcard-dns.sh <app-hostname> <traefik-ingress-tailscale-ip>
# Example: ./add-wildcard-dns.sh myapp.example.com 192.0.2.10
set -euo pipefail

APP_HOSTNAME="${1:?Usage: $0 <app-hostname> <traefik-ingress-tailscale-ip>}"
INGRESS_IP="${2:?Usage: $0 <app-hostname> <traefik-ingress-tailscale-ip>}"
CONF="/etc/dnsmasq.d/02-arcorefleet.conf"

echo "address=/${APP_HOSTNAME}/${INGRESS_IP}" | sudo tee -a "${CONF}" >/dev/null
sudo pihole restartdns

echo "Local DNS configured: ${APP_HOSTNAME} -> ${INGRESS_IP}"
