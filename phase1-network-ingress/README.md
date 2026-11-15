# Phase 1 — Hybrid Network & Ingress

App hostnames live directly under the root domain (`appname.example.com`),
not under a `dev.` prefix — Cloudflare's free Universal SSL cert only covers
one level of subdomain (`example.com` and `*.example.com`), not a
second level like `*.dev.example.com`. Covering that would require the
paid Advanced Certificate Manager add-on, so app hostnames stay one level
deep instead.

Execution order:

0. **DNS migration** — `example.com` lives at Namecheap with live
   website/email records. Cloudflare's self-serve onboarding won't accept a
   subdomain-only zone, so the whole domain moves to Cloudflare with all
   existing records kept DNS-only/unproxied (zero behavior change). See
   `cloudflare-dns/README.md` and do this first; everything below assumes
   `example.com` is already an Active Cloudflare zone.
1. **Tailscale mesh** — run `tailscale/join-tailscale.sh` on every node (homelab
   Ubuntu boxes + Oracle ARM node). Paste `tailscale/tailscale-acl.json` into
   the Tailscale admin console's ACL policy editor first so the tags are
   authorized.
2. **Traefik** — on the node you want acting as ingress, run
   `docker compose -f traefik/docker-compose.yml up -d`. Check which host
   ports are already taken first — on `fleet-control` an existing
   `sandbox-traefik` stack already owns 80/443, so this Traefik binds 8880
   (web) and loopback 8080 (dashboard) instead; adjust per node.
3. **Cloudflare Tunnel** — on the same ingress node, run
   `cloudflared/install-cloudflared.sh`. It installs cloudflared, creates the
   tunnel, routes `*.example.com` to it, and registers it as a systemd
   service pointing at Traefik on `localhost:8880`.
4. **Cloudflare Access (optional email gate)** — see
   `cloudflare-access/README.md` to require a Google or Microsoft sign-in
   before any `*.example.com` request reaches Traefik.
5. **Pi-hole** — on the Pi-hole host, run
   `pihole/add-wildcard-dns.sh <app-hostname> <ingress-node-tailscale-ip>`
   per app so internal clients resolve that hostname straight to the
   ingress node over the tailnet instead of leaving the network. This is
   deliberately per-app, not domain-wide — see the warning in that script.

Per-app routes are added as files in `traefik/dynamic/` (see
`sample-app.yml.example`) until Phase 2's K3s cluster takes over ingress via
native Ingress/IngressRoute objects.

## Critical: keep `www` and `sandbox` unproxied

`www.example.com` (Vercel) and `sandbox.example.com` (the existing
sandbox stack on `fleet-control`) must stay **DNS only** in Cloudflare.
They sit at the same subdomain level as every new app hostname, so if either
is ever switched to Proxied, it would start matching the `*.example.com`
wildcard used by both the Tunnel and the Access policy — including getting
gated behind the Google/Microsoft login.
