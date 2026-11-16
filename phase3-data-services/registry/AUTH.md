# Registry authentication rollout

The registry deployment now requires a `registry-auth` Secret containing a
`htpasswd` key. Missing credentials stop the new pod rather than allowing
anonymous writes. Do not apply the deployment before completing preparation.

1. Inventory every producer and consumer: Fleet CI, app-repository CI, every
   K3s node, local Docker clients and any rollback images. Schedule the rollout
   together; anonymous pulls stop as well as anonymous pushes.
2. Generate separate long random credentials for CI and nodes. On a trusted
   administration host with `htpasswd`, use `umask 077` and
   `htpasswd -Bc .htpasswd fleet-ci` (interactive password prompt), then
   `htpasswd -B .htpasswd fleet-node`. Store the passwords in the secret manager.
   Do not put passwords in command arguments, Git or workflow logs.
3. Create the cluster Secret from that local file:
   `kubectl -n data create secret generic registry-auth --from-file=htpasswd=.htpasswd`.
   Rotate an existing Secret through the cluster's secret-management process.
   Restart the registry after rotations; htpasswd is loaded at startup.
4. Set `REGISTRY_USERNAME` and `REGISTRY_PASSWORD` GitHub Actions secrets on
   each producing repository. The updated Fleet workflows write an isolated
   Docker auth config and remove it after use. App repositories using the
   workflow template must also copy `tools/configure-registry-auth.py`.
   Fleet deployment jobs require the repository variable `FLEET_DEPLOY_ENABLED`
   to be `true`. Keep it unset until credentials and node access are ready;
   security tests continue to run while deployment is disabled. Once rollout
   prerequisites are verified, enable it and dispatch the deployment workflows
   from main. Merging/publication alone must not start this infrastructure rollout.
5. On **each** K3s node, securely merge the following into the existing
   `/etc/rancher/k3s/registries.yaml`, preserving its other registries/mirrors.
   Replace the placeholders locally and protect the file with mode 0600:

   ```yaml
   configs:
     "<registry-tailnet-ip>:30500":
       auth:
         username: "<node-username>"
         password: "<node-password>"
   ```

   Restart K3s nodes one at a time, maintaining control-plane availability.
   Do not rerun the old configure-node.sh over this file: it now refuses to
   overwrite existing configuration. Configure other Docker clients through
   their credential stores as well.
6. Apply the registry deployment. Verify anonymous `/v2/` requests return 401,
   authenticated CI pushes work, and every node can pull a disposable image
   **by digest**. Test a cached-image restart and a fresh pull. Re-run these
   checks after credential rotation.

Basic htpasswd authentication grants registry-wide access, including writes;
it does not provide read-only node accounts or per-repository scopes. Separate
credentials allow independent rotation, not privilege separation. A scoped
token authorization service is the follow-up for least-privilege node pulls.

Current clients use HTTP over the tailnet. Tailscale encrypts its tunnel, but
HTTP/basic auth is not independently encrypted, including any in-cluster hops.
Restrict the NodePort on **all** node interfaces and prevent untrusted workloads
from reaching the registry. Before using a route outside this trusted/encrypted
boundary, configure registry TLS and trusted CAs on Docker/BuildKit/containerd;
never use `insecure_skip_verify` as a substitute. Test denial from a public
network and an unauthorized LAN client. No Ingress route is not a firewall.

Deployment workflows use the multi-platform image digest emitted by Buildx.
An attacker replacing a tag cannot change those selected bytes. Authentication
and digest deployment do not prevent image deletion or compromised build inputs.

If the rollout fails, restore known-good credential configuration and image
digests. Do not resolve failed pulls by disabling registry authentication.
