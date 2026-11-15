"""Calls into fleet apps on behalf of a Caller.

Requests go straight to each app's in-cluster Service (apps.json), not through
the public Ingress, so Cloudflare Access and the portal's forwardAuth are not
on this path. That is why every call here is checked against the Caller's
portal-granted hostnames first, and carries a fleet token naming the Caller.
"""

import json
import logging
import time
from typing import Any

import httpx

from .auth import Caller
from .config import App
from .signing import HEADER, Signer

logger = logging.getLogger('fleet_mcp.audit')

METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')
SCHEMA_CACHE_SECONDS = 300
MAX_BODY_CHARS = 100_000


class GatewayError(Exception):
    """A failure the caller can act on; shown to the model as the tool's error."""


class Gateway:
    def __init__(self, apps: tuple[App, ...], signer: Signer, client: httpx.AsyncClient):
        self.apps = {a.id: a for a in apps}
        self.signer = signer
        self.client = client
        self._schemas: dict[str, tuple[float, dict]] = {}

    def available(self, caller: Caller) -> list[App]:
        return [a for a in self.apps.values() if a.hostname.lower() in caller.hostnames]

    def app_for(self, caller: Caller, app_id: str) -> App:
        app = self.apps.get(app_id)
        if app is None or app.hostname.lower() not in caller.hostnames:
            ids = ', '.join(a.id for a in self.available(caller)) or 'none'
            raise GatewayError(f'No app "{app_id}" available to {caller.email}. Available: {ids}.')
        return app

    async def request(
        self, caller: Caller, app_id: str, method: str, path: str,
        query: dict[str, Any] | None = None, body: Any = None,
    ) -> dict:
        app = self.app_for(caller, app_id)
        method = method.upper()
        if method not in METHODS:
            raise GatewayError(f'method must be one of {", ".join(METHODS)}')
        # Only a path on this app: never a URL, so a call cannot be pointed at another host.
        if not path.startswith('/') or path.startswith('//') or '://' in path or '\\' in path:
            raise GatewayError('path must be an absolute path on the app, like /api/v1/projects/')

        started = time.monotonic()
        try:
            resp = await self.client.request(
                method, app.upstream + path, params=query or None,
                content=None if body is None else json.dumps(body),
                headers=self._headers(caller, app, json_body=body is not None),
                follow_redirects=False, timeout=30,
            )
        except httpx.HTTPError as exc:
            logger.warning('call email=%s key=%r app=%s %s %s error=%s',
                           caller.email, caller.key_name, app.id, method, path, exc)
            raise GatewayError(f'{app.name} did not answer: {exc}') from exc
        logger.info('call email=%s key=%r app=%s %s %s status=%s ms=%d', caller.email, caller.key_name,
                    app.id, method, path, resp.status_code, (time.monotonic() - started) * 1000)
        return _result(resp)

    async def schema(self, caller: Caller, app_id: str) -> dict:
        app = self.app_for(caller, app_id)
        hit = self._schemas.get(app.id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            resp = await self.client.get(app.upstream + app.schema, headers=self._headers(caller, app), timeout=30)
            resp.raise_for_status()
            schema = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GatewayError(f'Could not read the API schema of {app.name}: {exc}') from exc
        self._schemas[app.id] = (time.monotonic() + SCHEMA_CACHE_SECONDS, schema)
        return schema

    def _headers(self, caller: Caller, app: App, json_body: bool = False) -> dict[str, str]:
        headers = {
            # The app sees its own public hostname, as it would through the Ingress.
            'Host': app.hostname,
            'X-Forwarded-Host': app.hostname,
            'X-Forwarded-Proto': 'https',
            'Accept': 'application/json',
            HEADER: self.signer.sign(caller.email, app.hostname),
        }
        if json_body:
            headers['Content-Type'] = 'application/json'
        return headers


def _result(resp: httpx.Response) -> dict:
    result: dict[str, Any] = {'status': resp.status_code}
    if resp.is_redirect:
        result['location'] = resp.headers.get('location', '')
        result['hint'] = 'Redirects are not followed. For Django apps, add the trailing slash to the path.'
    text = resp.text
    if 'json' in resp.headers.get('content-type', ''):
        try:
            result['body'] = resp.json()
            if len(text) > MAX_BODY_CHARS:
                result.pop('body')
                result['body_text'] = text[:MAX_BODY_CHARS]
                result['truncated'] = True
            return result
        except ValueError:
            pass
    if text:
        result['body_text'] = text[:MAX_BODY_CHARS]
        result['truncated'] = len(text) > MAX_BODY_CHARS
    return result


def list_operations(schema: dict, search: str | None = None) -> list[dict]:
    """One line per operation: method, path and summary, optionally filtered by a search term."""
    needle = (search or '').lower()
    ops = []
    for path, item in schema.get('paths', {}).items():
        for method, op in item.items():
            if method.upper() not in METHODS or not isinstance(op, dict):
                continue
            summary = op.get('summary') or op.get('operationId') or ''
            if needle and needle not in f'{path} {summary} {" ".join(op.get("tags", []))}'.lower():
                continue
            ops.append({'method': method.upper(), 'path': path, 'summary': summary})
    return ops


def describe_operation(schema: dict, method: str, path: str) -> dict:
    """The parameters, request body and responses of one operation, with $refs inlined."""
    item = schema.get('paths', {}).get(path)
    op = (item or {}).get(method.lower())
    if not op:
        raise GatewayError(f'{method.upper()} {path} is not in the schema; use list_endpoints to find it.')
    params = [*item.get('parameters', []), *op.get('parameters', [])]
    out = {
        'method': method.upper(), 'path': path,
        'summary': op.get('summary', ''), 'description': op.get('description', ''),
        'parameters': _inline(schema, params),
        'request_body': _inline(schema, op.get('requestBody')),
        'responses': _inline(schema, {code: r for code, r in op.get('responses', {}).items() if code.startswith('2')}),
    }
    return {k: v for k, v in out.items() if v not in ('', [], None, {})}


def _inline(schema: dict, node: Any, depth: int = 0, seen: frozenset = frozenset()) -> Any:
    if isinstance(node, list):
        return [_inline(schema, n, depth, seen) for n in node]
    if not isinstance(node, dict):
        return node
    ref = node.get('$ref')
    if isinstance(ref, str) and ref.startswith('#/'):
        if ref in seen or depth > 6:
            return {'$ref': ref}
        target: Any = schema
        for part in ref[2:].split('/'):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        return _inline(schema, target, depth + 1, seen | {ref})
    return {k: _inline(schema, v, depth, seen) for k, v in node.items()}
