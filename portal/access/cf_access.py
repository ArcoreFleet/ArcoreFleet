"""Identity from Cloudflare Access.

Every request reaches the fleet through Cloudflare Access, which signs users
in with Google or Microsoft and forwards a signed JWT in the
Cf-Access-Jwt-Assertion header. Verifying it (signature against the team's
published keys, audience = the Access app's AUD tag, issuer = the team
domain) yields the user's email.

Enabled only when CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD are set.
"""

import logging
import os
import time

import requests
from django.contrib.auth import get_user_model, login, logout
from django.http import HttpResponseForbidden
from django.utils.crypto import get_random_string
from jose import JWTError, jwt

from .models import access_for

logger = logging.getLogger(__name__)

HEADER = 'Cf-Access-Jwt-Assertion'
LOGOUT_URL = '/cdn-cgi/access/logout'
# /auth/check verifies tokens itself and must not create a session per request;
# /api/keys/resolve is called by fleet-mcp, never by a browser.
SKIP_PATHS = ('/auth/check', '/healthz', '/api/keys/resolve')
JWKS_CACHE: dict[str, object] = {'keys': None, 'expires_at': 0.0}


def settings_from_env() -> tuple[str, str]:
    return os.getenv('CF_ACCESS_TEAM_DOMAIN', '').rstrip('/'), os.getenv('CF_ACCESS_AUD', '')


def is_enabled() -> bool:
    team, aud = settings_from_env()
    return bool(team and aud)


def _get_jwks(team: str, refresh: bool = False) -> dict:
    now = time.time()
    if not refresh and JWKS_CACHE['keys'] and JWKS_CACHE['expires_at'] > now:
        return JWKS_CACHE['keys']  # type: ignore[return-value]

    resp = requests.get(f'{team}/cdn-cgi/access/certs', timeout=5)
    resp.raise_for_status()
    JWKS_CACHE['keys'] = {'keys': resp.json()['keys']}
    JWKS_CACHE['expires_at'] = now + int(os.getenv('CF_ACCESS_JWKS_CACHE_SECONDS', '3600'))
    return JWKS_CACHE['keys']  # type: ignore[return-value]


def verify_token(token: str) -> dict:
    team, aud = settings_from_env()
    try:
        return jwt.decode(token, _get_jwks(team), algorithms=['RS256'], audience=aud, issuer=team)
    except JWTError:
        # Cloudflare rotates its signing keys; retry once with fresh ones.
        return jwt.decode(token, _get_jwks(team, refresh=True), algorithms=['RS256'], audience=aud, issuer=team)


def email_from_request(request) -> str | None:
    """The verified Access email for this request, or None."""
    token = request.headers.get(HEADER)
    if not token:
        return None
    try:
        return verify_token(token).get('email') or None
    except (JWTError, requests.RequestException, KeyError) as exc:
        logger.warning('cf_access: rejected token: %s', exc)
        return None


def _admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.getenv('CF_ACCESS_ADMIN_EMAILS', '').split(',') if e.strip()}


def ensure_user(email: str):
    """The user for this email, created on first sight (with the default access)."""
    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        base = email.split('@')[0]
        username = base if not User.objects.filter(username=base).exists() else f'{base}-{get_random_string(4)}'
        user = User(username=username, email=email.lower())
        user.set_unusable_password()
        user.save()
        logger.info('cf_access: provisioned user %s for %s', username, email)
    access_for(user)

    # Admin emails are promoted, never demoted - admins set in the portal stay admins.
    if email.lower() in _admin_emails() and not (user.is_staff and user.is_superuser):
        user.is_staff = user.is_superuser = True
        user.save(update_fields=['is_staff', 'is_superuser'])
    return user


class CloudflareAccessMiddleware:
    """Logs in the user identified by Cloudflare Access. Place after AuthenticationMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        team, aud = settings_from_env()
        if (team or aud) and request.path not in SKIP_PATHS:
            # A partial SSO configuration must not silently enable local login.
            if not (team and aud) or not self._sync_user(request):
                logout(request)
                return HttpResponseForbidden('A valid Cloudflare Access sign-in is required.')
        return self.get_response(request)

    def _sync_user(self, request):
        email = email_from_request(request)
        if not email:
            return False
        if request.user.is_authenticated and request.user.email.lower() == email.lower():
            return request.user.is_active
        user = ensure_user(email)
        if not user.is_active:
            return False
        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        return True
