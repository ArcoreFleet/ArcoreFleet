"""Executed inside the existing portal pod; credentials arrive only on stdin."""
import json
import os
import secrets
import sys
from urllib.parse import parse_qs, urlsplit

import requests


def oauth_settings(current, oidc):
    settings = dict(current)
    settings['AuthenticationMethod'] = 3
    settings['OAuthSettings'] = {
        **settings.get('OAuthSettings', {}),
        'ClientID': oidc['client_id'], 'ClientSecret': oidc['client_secret'],
        'AuthorizationURI': oidc['authorization_url'], 'AccessTokenURI': oidc['token_url'],
        'ResourceURI': oidc['userinfo_url'], 'RedirectURI': oidc['redirect_uri'],
        'LogoutURI': oidc['logout_url'], 'UserIdentifier': 'email',
        # Portainer embeds this string directly in its public authorization URL.
        # OAuth scopes are space-delimited; commas become one invalid scope.
        'Scopes': 'openid email profile', 'AuthStyle': 1, 'SSO': True,
        'OAuthAutoCreateUsers': False,
    }
    return settings


def configure_portainer(oidc, bootstrap, emails):
    if not emails:
        raise ValueError('At least one active portal admin email is required.')
    base = 'http://portainer.portainer.svc.cluster.local:9000/api'
    session = requests.Session()

    def call(method, path, payload=None):
        response = session.request(method, base + path, json=payload, timeout=30, allow_redirects=False)
        if response.status_code >= 300:
            raise ValueError('Portainer request failed; response suppressed.')
        return response.json() if response.content else None

    auth = call('POST', '/auth', {'Username': bootstrap['username'], 'Password': bootstrap['password']})
    # API automation uses the bearer token. Portainer also returns a browser
    # cookie, which would select cookie authentication and require browser CSRF.
    session.cookies.clear()
    session.headers['Authorization'] = 'Bearer ' + auth['jwt']
    current = call('GET', '/settings')
    users = {user['Username']: user for user in call('GET', '/users')}
    # Explicitly provision only portal admins. No wildcard user/admin auto-provisioning.
    for email in emails:
        user = users.get(email)
        if user is None:
            call('POST', '/users', {'Username': email, 'Password': secrets.token_urlsafe(32), 'Role': 1})
        elif user['Role'] != 1:
            call('PUT', '/users/' + str(user['Id']), {'Role': 1})
    desired = oauth_settings(current, oidc)
    call('PUT', '/settings', desired)
    actual = call('GET', '/settings')
    if actual['AuthenticationMethod'] != 3 or actual['OAuthSettings']['ClientID'] != oidc['client_id']:
        raise ValueError('Portainer did not retain the requested OAuth configuration.')
    public = call('GET', '/settings/public')
    query = parse_qs(urlsplit(public['OAuthLoginURI']).query)
    if (query.get('scope', [''])[0].split() != ['openid', 'email', 'profile']
            or query.get('client_id') != [oidc['client_id']]
            or query.get('redirect_uri') != [oidc['redirect_uri']]):
        raise ValueError('Portainer generated an unexpected OAuth authorization URL.')


def main():
    try:
        payload = json.load(sys.stdin)
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'portal.settings')
        import django
        django.setup()
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from access.models import Application
        app = payload['app']
        if app.get('adapter') == 'portainer' and not app['admin_only']:
            raise ValueError('Portainer must remain admin only.')
        Application.objects.update_or_create(hostname=app['hostname'], defaults={
            'name': app['name'], 'admin_only': app['admin_only'],
        })
        cache.clear()
        if payload.get('configure_adapter'):
            if app.get('adapter') != 'portainer':
                raise ValueError('Unknown adapter.')
            emails = list(get_user_model().objects.filter(is_active=True, is_staff=True).exclude(email='')
                          .values_list('email', flat=True))
            configure_portainer(payload['oidc'], payload['bootstrap'], [email.lower() for email in emails])
        print('Application reconciled.')
    except Exception:
        # Remote tracebacks must never include OAuth credentials or bootstrap passwords.
        raise SystemExit('Application SSO configuration failed; details suppressed.') from None


if __name__ == '__main__':
    main()
