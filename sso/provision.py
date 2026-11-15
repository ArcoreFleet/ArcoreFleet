"""Reconcile approved app registrations. No daemon, plaintext artifacts or secret logs."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

import requests


class ProvisionError(Exception):
    pass


def validate(config):
    team = urlsplit(config['team_domain'])
    if team.scheme != 'https' or not re.fullmatch(r'[a-z0-9-]+\.cloudflareaccess\.com', team.netloc) or team.path:
        raise ProvisionError('Use the HTTPS Cloudflare team origin.')
    wildcard = config['wildcard_domain']
    if not re.fullmatch(r'\*\.[a-z0-9.-]+', wildcard):
        raise ProvisionError('Invalid wildcard domain.')
    ids, hosts = set(), set()
    for app in config['apps']:
        slug, host = app['id'], app['hostname']
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', slug) or slug in ids or host in hosts:
            raise ProvisionError('App IDs and hostnames must be valid and unique.')
        if not re.fullmatch(r'[a-z0-9-]+' + re.escape(wildcard[1:]), host):
            raise ProvisionError('Hostnames must be directly covered by the wildcard.')
        if app['mode'] not in ('cloudflare_jwt', 'oidc') or type(app['admin_only']) is not bool:
            raise ProvisionError('Choose a supported mode and explicit admin_only boolean.')
        if not app.get('name') or len(app['name']) > 100:
            raise ProvisionError('A short application name is required.')
        if app['mode'] == 'oidc':
            if not app.get('redirect_uris'):
                raise ProvisionError('OIDC requires explicit redirect URIs.')
            for uri in app['redirect_uris']:
                parsed = urlsplit(uri)
                if parsed.scheme != 'https' or parsed.netloc != host or parsed.fragment or parsed.query:
                    raise ProvisionError('Redirect URIs must use the application HTTPS hostname.')
            if app.get('adapter') not in (None, 'portainer'):
                raise ProvisionError('Unknown application adapter.')
            if app.get('adapter') == 'portainer':
                if not app['admin_only'] or host != 'portainer.example.com' or app['redirect_uris'] != ['https://' + host + '/']:
                    raise ProvisionError('Portainer requires admin-only access and its root callback.')
                if not re.fullmatch(r'[a-z0-9-]+', app.get('bootstrap_secret', '')):
                    raise ProvisionError('Portainer requires a bootstrap Secret name.')
        ids.add(slug)
        hosts.add(host)
    return config


class Kube:
    def run(self, args, payload=None):
        result = subprocess.run(['kubectl', *args], input=payload, text=True, capture_output=True)
        if result.returncode:
            # kubectl errors can repeat Secret manifests or remote tracebacks.
            raise ProvisionError('Kubernetes operation failed; output suppressed to protect credentials.')
        return result.stdout

    def secret(self, name, namespace='fleet-sso'):
        text = self.run(['-n', namespace, 'get', 'secret', name, '--ignore-not-found', '-o', 'json'])
        if not text.strip():
            return None
        obj = json.loads(text)
        return {key: base64.b64decode(value).decode() for key, value in obj.get('data', {}).items()}

    def save(self, name, values):
        obj = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {
            'name': name, 'namespace': 'fleet-sso', 'labels': {'app.kubernetes.io/managed-by': 'fleet-sso'},
        }, 'type': 'Opaque', 'stringData': values}
        # Server-side apply avoids a last-applied annotation containing plaintext.
        self.run(['apply', '--server-side', '--field-manager=fleet-sso', '-f', '-'], json.dumps(obj))

    def portal(self, payload, adapter=False):
        path = Path(__file__).with_name('portal_adapter.py')
        return self.run(['-n', 'apps', 'exec', '-i', 'deployment/portal', '--', 'python', '-c', path.read_text()],
                        json.dumps({**payload, 'configure_adapter': adapter}))


class Cloudflare:
    def __init__(self, account, token):
        if not re.fullmatch(r'[a-fA-F0-9]{32}', account):
            raise ProvisionError('Invalid Cloudflare account ID.')
        self.account = account
        self.base = f'https://api.cloudflare.com/client/v4/accounts/{account}/access/apps'
        self.session = requests.Session()
        self.session.headers['Authorization'] = 'Bearer ' + token

    def call(self, method, suffix='', payload=None, params=None):
        try:
            response = self.session.request(method, self.base + suffix, json=payload, params=params,
                                            timeout=30, allow_redirects=False)
            if response.status_code >= 300:
                raise ProvisionError(f'Cloudflare {method} failed (HTTP {response.status_code}); response suppressed.')
            body = response.json()
            if not body.get('success'):
                raise ProvisionError('Cloudflare rejected the request; response suppressed.')
            return body
        except (requests.RequestException, ValueError):
            raise ProvisionError('Cloudflare request failed; details suppressed.') from None

    def applications(self):
        page = 1
        while True:
            body = self.call('GET', params={'page': page, 'per_page': 100})
            yield from body['result']
            if page >= body.get('result_info', {}).get('total_pages', 1):
                return
            page += 1


def registration(app, wildcard):
    idps = wildcard.get('allowed_idps')
    # Clone only explicitly understood policy fields; never create an allow-everyone policy.
    policies = wildcard.get('policies', [])
    supported = {'name', 'decision', 'precedence', 'include', 'exclude', 'require',
                 'session_duration', 'approval_required', 'approval_groups', 'purpose_justification_required',
                 'purpose_justification_prompt', 'isolation_required', 'mfa_config', 'connection_rules'}
    if not idps or not policies or not any(p.get('decision') == 'allow' for p in policies):
        raise ProvisionError('The wildcard must have explicit identity providers and an allow policy.')
    copied = []
    for policy in policies:
        if policy.get('decision') not in ('allow', 'deny') or not policy.get('include'):
            raise ProvisionError('Only explicit allow/deny wildcard policies can be copied.')
        unknown = set(policy) - supported - {'id', 'uid', 'created_at', 'updated_at', 'reusable', 'app_count'}
        if unknown:
            raise ProvisionError('Wildcard policy has unsupported fields; review before provisioning.')
        copied.append({key: value for key, value in policy.items() if key in supported})
    return {'name': 'Fleet SSO: ' + app['id'], 'type': 'saas', 'allowed_idps': idps,
            'policies': copied, 'app_launcher_visible': False,
            'saas_app': {'auth_type': 'oidc', 'redirect_uris': app['redirect_uris'],
                         'grant_types': ['authorization_code'], 'scopes': ['openid', 'email', 'profile'],
                         'access_token_lifetime': '15m'}}


def reconcile_oidc(cf, kube, app, wildcard, team, applications):
    name = 'fleet-sso-' + app['id']
    state = kube.secret(name)
    matches = [item for item in applications if item.get('name') == 'Fleet SSO: ' + app['id']]
    if len(matches) > 1:
        raise ProvisionError('Duplicate Cloudflare app names; resolve manually.')
    payload = registration(app, wildcard)
    if state:
        if state.get('account_id') != cf.account or state.get('hostname') != app['hostname']:
            raise ProvisionError('Saved OIDC state belongs to a different account or hostname.')
        existing = cf.call('GET', '/' + state['app_id'])['result']
        if existing.get('type') != 'saas' or existing.get('name') != payload['name'] or existing.get('saas_app', {}).get('client_id') != state['client_id']:
            raise ProvisionError('Saved OIDC state does not match Cloudflare; refusing replacement.')
        payload['saas_app']['client_id'] = state['client_id']
        updated = cf.call('PUT', '/' + state['app_id'], payload)['result']
        if updated.get('saas_app', {}).get('client_id') != state['client_id']:
            raise ProvisionError('Cloudflare unexpectedly changed the client identity; stop and recover saved state.')
    else:
        if matches:
            # Cloudflare returns client_secret only on creation. Never silently rotate/recreate.
            raise ProvisionError('OIDC app already exists without saved state; restore its Kubernetes Secret.')
        result = cf.call('POST', payload=payload)['result']
        saas = result['saas_app']
        state = {'app_id': result['id'], 'client_id': saas['client_id'], 'client_secret': saas['client_secret'],
                 'account_id': cf.account, 'hostname': app['hostname']}
        kube.save(name, state)
    root = team + '/cdn-cgi/access/sso/oidc/' + state['client_id']
    state.update({'authorization_url': root + '/authorization', 'token_url': root + '/token',
                  'userinfo_url': root + '/userinfo', 'redirect_uri': app['redirect_uris'][0],
                  'logout_url': team + '/cdn-cgi/access/logout'})
    kube.save(name, state)
    return state


def apply(config, cf, kube):
    applications = list(cf.applications())
    matches = [item for item in applications if item.get('domain') == config['wildcard_domain'] and item.get('type') == 'self_hosted']
    if len(matches) != 1:
        raise ProvisionError('Expected exactly one existing wildcard Access application.')
    wildcard = cf.call('GET', '/' + matches[0]['id'])['result']
    wildcard['policies'] = cf.call('GET', '/' + matches[0]['id'] + '/policies')['result']
    # Preflight bootstrap access and policy support before any changes.
    bootstraps = {}
    for app in config['apps']:
        if app['mode'] == 'oidc':
            registration(app, wildcard)
        if app.get('adapter') == 'portainer':
            bootstraps[app['id']] = kube.secret(app['bootstrap_secret'], 'portainer')
            if not bootstraps[app['id']] or not all(bootstraps[app['id']].get(k) for k in ('username', 'password')):
                raise ProvisionError('Portainer bootstrap Secret is missing username/password.')
    for app in config['apps']:
        # Ensure portal authorization exists before enabling any application login.
        kube.portal({'app': app})
        if app['mode'] == 'oidc':
            state = reconcile_oidc(cf, kube, app, wildcard, config['team_domain'], applications)
            if app.get('adapter') == 'portainer':
                kube.portal({'app': app, 'oidc': state, 'bootstrap': bootstraps[app['id']]}, adapter=True)
        print(app['id'] + ': reconciled')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='sso/apps.json')
    parser.add_argument('--apply', action='store_true', help='Reconcile Cloudflare, Kubernetes and portal settings.')
    args = parser.parse_args()
    try:
        config = validate(json.loads(Path(args.config).read_text()))
        if not args.apply:
            for app in config['apps']:
                print(f"{app['id']}: {app['mode']}, admin_only={app['admin_only']}")
            return
        token, account = os.getenv('CLOUDFLARE_API_TOKEN'), os.getenv('CLOUDFLARE_ACCOUNT_ID')
        if not token or not account:
            raise ProvisionError('Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID before applying.')
        apply(config, Cloudflare(account, token), Kube())
    except ProvisionError as exc:
        raise SystemExit('SSO provisioning failed: ' + str(exc)) from None
    except (KeyError, TypeError, ValueError, OSError):
        # Exceptions may carry a subprocess input or API response containing credentials.
        raise SystemExit('SSO provisioning failed. Check configuration, bootstrap access and saved client state; no credentials were logged.') from None


if __name__ == '__main__':
    main()
