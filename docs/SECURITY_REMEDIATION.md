# Security remediation checklist

Review date: 2026-11-15. Applies to the development source and its sanitized
public snapshot. Checked source changes do not mean the live fleet is updated.

## Source fixes

- [x] Require a valid Cloudflare Access assertion on every SSO browser request,
  including requests carrying an existing Django session. Reject partial SSO
  configuration and inactive/mismatched identities; preserve machine endpoints.
- [x] Upgrade the portal to supported Django 5.2 LTS and run its full test suite.
- [x] Default session and CSRF cookies to Secure; document local HTTP opt-out
  and explicit trusted-proxy configuration.
- [x] Run portal and Oracle sync containers as non-root; enforce the portal pod
  security context and document mounted-file permissions.
- [x] Pin third-party CI actions to full commit SHAs, including public overlays
  and deployment workflow templates.
- [x] Deploy images using the build output digest, not a mutable SHA-shaped tag.
- [x] Prepare registry authentication and a coordinated client rollout.
- [x] Harden Oracle SQL identifier quoting and cover unusual identifiers.
- [x] Record secret-scanner false positives without broad rule suppression.
- [x] Enforce LF shell-script checkout and smoke-test the actual container entrypoint.
- [x] Atomically replace registry auth files using a private temporary file;
  preserve the prior file and remove temporary credentials on replacement failure.
- [x] Update the installed operations collector with the same portal image digest,
  after portal migrations, so dependency/security fixes reach that worker too.
- [x] Prepare a NodePort address-filter drop-in and staged rollback procedure:
  phase2-k3s-portainer/NODEPORT-HARDENING.md.

## Deployment and publication

- [ ] Provision registry credentials, configure every CI producer and pulling node,
  then enable registry authentication. Verify anonymous pulls/pushes fail.
- [ ] Verify registry traffic is confined to encrypted/trusted paths; basic auth
  over plain HTTP is not transport encryption. TLS is required beyond the tailnet.
- [ ] Restrict NodePort reachability on every node; verify 30480, 30443, 30500
  and management ports from an external network and an unauthorized LAN client.
  No public Ingress rule does not imply no direct NodePort access.
- [ ] Build and smoke-test containers, including mounted sync.env/wallet files
  readable by UID/GID 10001, migrations, probes, workers and the TLS authority.
  Both architectures now pass isolated startup tests; real sync/wallet mounts and
  live database operations remain unverified.
- [ ] Deploy and verify SSO, logout, revocation, secure cookies and digest pulls.
- [ ] Publish sanitized snapshots from reviewed committed source; rescan the
  public repositories and confirm the published commit IDs.

## Scanner triage

The reviewed public Fleet revision was 57ef8d0. Gitleaks 8.30.1 matched
the Ed25519PrivateKey type annotation in portal/hubauthority/signing.py and the expression
`lease_until=now` in portal/access/collector.py and portal/website/sync.py.
These are Python code, not embedded credentials. SafeWeave's hidden finding
locations were unavailable, so its individual critical finding remains unmapped.
Do not disable generic secret detection globally or rotate unrelated keys on
the strength of those syntax matches alone.

Publication scanning is enabled. The reviewed type annotation has a single-line
gitleaks allow comment; collector calls are formatted so separate keyword
arguments are not mistaken for an embedded credential. No files or rules are
globally excluded from secret scanning.

The sync SQL uses schema-derived identifiers, not web request input. Keep its
source credentials read-only where possible and bind all copied data values.

## Related Hub work

Hub has its own docs/SECURITY_REMEDIATION.md. Keep its production root/spoke gates
closed until the existing production authorization milestones are implemented.

## Validation of this change

- Portal: 116 tests passed on Python 3.11 / Django 5.2.19, including seven new
  SSO regression tests. No model migrations required.
- SSO provisioning: 17 tests passed. SQL identifiers and registry credential
  handling: four tests passed.
- Actionlint passed for both repositories' workflows, the public CI overlay and
  the app deployment template. All 44 YAML files/templates parsed; action refs
  are full SHAs resolved from upstream tags.
- Both sanitized public snapshots passed the existing publication deny/require
  checks using isolated temporary Git repositories containing these source edits.
- Docker's Linux engine was unavailable locally. Isolated Docker image builds and
  smoke tests subsequently passed on both AMD64 and ARM64 Fleet hosts: non-root
  portal migrations/startup/health, authority startup with certificate-verified
  TLS, sync driver imports/CLI, and worker command initialization with a read-only
  filesystem. Tests used no production credentials, no network access and no
  published ports. Private and public CI now run the same smoke script.
- Three credential-file tests passed on Windows and in a non-root Linux container,
  including restrictive permissions and cleanup/preservation after a failed replace.
- Read-only live checks: anonymous registry `/v2/` returned HTTP 200. Portal,
  authority and registry NodePorts accepted TCP connections on the worker LAN IP.
  The corresponding Oracle public-IP probes timed out from the review workstation;
  this observation does not establish a permanent firewall guarantee.
- The local kubeconfig points at a different cluster. Use the verified Fleet SSH
  hosts for rollout. The worker SSH account cannot run passwordless sudo, so the
  prepared NodePort change requires an administrator. Registry CI secrets are also
  absent. No production workload, GitHub secret or network policy was changed.
- Source fixes are tracked on the development repository's chore/public-publish
  branch. Public snapshot publication and live deployment remain separate steps.
