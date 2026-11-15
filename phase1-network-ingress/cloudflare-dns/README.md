# example.com — full migration to Cloudflare DNS

Cloudflare's self-serve "Connect your domain" flow rejects subdomain-only
zones (it hard-blocks anything but a root domain), so the subdomain
delegation approach isn't available. Instead, the whole `example.com`
zone moves to Cloudflare — but every existing record is kept **DNS only**
(grey cloud / unproxied), so Cloudflare just answers DNS identically to
Namecheap today. Only the new `dev` tunnel record gets proxied. Nothing
about the live website or email changes in behavior.

Current live records (Namecheap), to preserve exactly:

| Type  | Host         | Value                                        |
|-------|--------------|-----------------------------------------------|
| A     | @            | 216.198.79.1                                  |
| A     | sandbox      | 192.0.2.10                                |
| CNAME | autodiscover | autodiscover.outlook.com. (Microsoft 365)     |
| CNAME | www          | 2632bf4dda07c6c2.vercel-dns-017.com. (Vercel)  |
| TXT   | @            | v=spf1 include:spf.protection.outlook.com -all|

## Steps

### 0. Safety net
Before changing anything, note Namecheap's current nameservers (Domain
List -> `example.com` -> Nameservers dropdown — likely
`dns1.registrar-servers.com` / `dns2.registrar-servers.com`, or whatever it
currently shows). If anything goes wrong after cutover, switching back to
these restores today's behavior immediately.

### 1. Add the root domain to Cloudflare
- Cloudflare dashboard -> **Connect your domain**
- Domain name: `example.com` (root domain, not `dev.example.com`)
- Leave the AI/bot preference section at defaults — irrelevant here
- **Import DNS records**: leave on **Automatic**

### 2. Verify the imported records before continuing
Cloudflare will show a scanned list of records. Check every one of the 5
rows in the table above is present and correct. If anything is missing or
wrong, fix/add it manually in this step — this is the only place to do it
before DNS authority moves.

Set proxy status (the orange/grey cloud toggle) to **DNS only (grey)** for
all 5 existing records — `@` (A), `sandbox` (A), `autodiscover` (CNAME),
`www` (CNAME), and the TXT record is never proxied anyway. This keeps
their behavior identical to Namecheap today.

### 3. Switch nameservers at Namecheap
Cloudflare gives you two nameservers for this zone (e.g.
`xxxx.ns.cloudflare.com` / `yyyy.ns.cloudflare.com`).
- Namecheap -> Domain List -> `example.com` -> **Nameservers** dropdown
  (not Advanced DNS) -> switch from Namecheap BasicDNS to **Custom DNS**
- Enter Cloudflare's two nameservers, save

This is the actual cutover moment — DNS authority for the whole domain
moves to Cloudflare at this point, once it propagates.

### 4. Confirm activation and verify nothing broke
- Cloudflare dashboard shows the zone as **Active** once it sees the NS
  change (minutes to a few hours)
- Verify: the website at `example.com`/`www.example.com` still loads
  (Vercel), and send a test email to confirm Microsoft 365 mail still
  flows correctly (SPF/autodiscover unaffected)

### 5. Continue with Phase 1
Once confirmed stable, run `cloudflared/install-cloudflared.sh` from the
parent directory as documented — `cloudflared tunnel route dns` will
create a `*.example.com` CNAME (proxied) inside this zone, and the
Cloudflare Access policy in `cloudflare-access/` will apply to it. App
hostnames live one level deep (`appname.example.com`), not under a
`dev.` prefix — see the note at the top of `../README.md` for why (free
Universal SSL only covers one level of subdomain).
