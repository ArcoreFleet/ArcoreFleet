"""API keys: the portal decides who a key belongs to and which apps they may use.

Every request to /mcp needs `Authorization: Bearer afk_...`. The key is checked
with the portal (POST /api/keys/resolve, in-cluster, with the shared secret)
and the answer cached briefly - the same 15 seconds the portal's forwardAuth
uses, so revoking a key or an app grant applies to both paths together.
"""

import hashlib
import logging
import time
from dataclasses import dataclass

import httpx
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

CACHE_SECONDS = 15


@dataclass(frozen=True)
class Caller:
    email: str
    is_admin: bool
    key_name: str
    hostnames: frozenset[str]


class PortalUnavailable(Exception):
    pass


class KeyResolver:
    def __init__(self, portal_url: str, shared_secret: str, client: httpx.AsyncClient):
        self._url = f'{portal_url}/api/keys/resolve'
        self._secret = shared_secret
        self._client = client
        self._cache: dict[str, tuple[float, Caller]] = {}

    async def resolve(self, raw_key: str) -> Caller | None:
        digest = hashlib.sha256(raw_key.encode()).hexdigest()
        now = time.monotonic()
        hit = self._cache.get(digest)
        if hit and hit[0] > now:
            return hit[1]

        try:
            resp = await self._client.post(
                self._url, json={'key': raw_key}, headers={'X-Fleet-MCP-Secret': self._secret}, timeout=5
            )
        except httpx.HTTPError as exc:
            raise PortalUnavailable(str(exc)) from exc
        if resp.status_code == 401:
            self._cache.pop(digest, None)
            return None
        if resp.status_code != 200:
            raise PortalUnavailable(f'portal answered {resp.status_code}')

        body = resp.json()
        caller = Caller(
            email=body['email'], is_admin=bool(body.get('is_admin')), key_name=body.get('key', ''),
            hostnames=frozenset(a['hostname'].lower() for a in body.get('apps', [])),
        )
        if len(self._cache) > 1000:
            self._cache.clear()
        self._cache[digest] = (now + CACHE_SECONDS, caller)
        return caller


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse({'error': detail}, status_code=401, headers={'WWW-Authenticate': 'Bearer'})


class BearerKeyMiddleware:
    """Requires a valid API key on the MCP endpoint and puts the Caller in request.state.caller.
    Health and JWKS stay public. Fails closed when the portal cannot be asked."""

    def __init__(self, app: ASGIApp, resolver: KeyResolver, protected_prefix: str = '/mcp'):
        self.app = app
        self.resolver = resolver
        self.prefix = protected_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http' or not scope['path'].startswith(self.prefix):
            await self.app(scope, receive, send)
            return

        headers = dict(scope['headers'])
        auth = headers.get(b'authorization', b'').decode()
        scheme, _, raw = auth.partition(' ')
        if scheme.lower() != 'bearer' or not raw.strip():
            await _unauthorized('An API key is required: Authorization: Bearer afk_... (create one at the Arcore Portal, API keys)')(scope, receive, send)
            return
        try:
            caller = await self.resolver.resolve(raw.strip())
        except PortalUnavailable as exc:
            logger.error('auth: portal unavailable: %s', exc)
            await JSONResponse({'error': 'The portal could not check this key; try again shortly.'}, status_code=503)(scope, receive, send)
            return
        if caller is None:
            await _unauthorized('Unknown or revoked API key')(scope, receive, send)
            return

        scope.setdefault('state', {})['caller'] = caller
        await self.app(scope, receive, send)
