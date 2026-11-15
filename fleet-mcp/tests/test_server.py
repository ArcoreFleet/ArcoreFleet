"""End to end: a real MCP client against the gateway under uvicorn, with the
portal and the apps replaced by an httpx MockTransport."""

import json
import socket
import threading
import time

import httpx
import httpx2
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from fleet_mcp import config, server
from fleet_mcp.signing import HEADER

KEY = 'afk_alice'
SECRET = 'shared'
ISSUER = 'https://mcp.example.com'

_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _private.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()

APPS = (
    config.App(id='thread', name='ArcoreThread', hostname='thread.example.com',
               upstream='http://thread.internal', schema='/openapi.json'),
    config.App(id='estate', name='ArcoreEstate', hostname='estate.example.com',
               upstream='http://estate.internal', schema='/api/schema/?format=json'),
)

SCHEMA = {
    'openapi': '3.1.0',
    'paths': {
        '/api/v1/projects/': {
            'get': {'summary': 'List Projects'},
            'post': {
                'summary': 'Create Project',
                'requestBody': {'content': {'application/json': {'schema': {'$ref': '#/components/schemas/ProjectCreate'}}}},
                'responses': {'200': {'description': 'ok'}, '422': {'description': 'invalid'}},
            },
        },
        '/healthz': {'get': {'summary': 'Liveness'}},
    },
    'components': {'schemas': {'ProjectCreate': {'type': 'object', 'properties': {'name': {'type': 'string'}}}}},
}


class Backend:
    """The portal plus the two apps, recording what reached them."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.portal_up = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == 'portal.internal':
            if not self.portal_up:
                return httpx.Response(502)
            assert request.headers['X-Fleet-MCP-Secret'] == SECRET
            if json.loads(request.content)['key'] != KEY:
                return httpx.Response(401, json={'error': 'unknown'})
            # Alice may use ArcoreThread only.
            return httpx.Response(200, json={'email': 'alice@example.com', 'is_admin': False, 'key': 'laptop',
                                             'apps': [{'hostname': 'thread.example.com'}]})
        if request.url.path == '/openapi.json':
            return httpx.Response(200, json=SCHEMA)
        if request.url.path == '/api/v1/projects':
            return httpx.Response(307, headers={'location': 'http://thread.example.com/api/v1/projects/'})
        if request.url.path == '/api/v1/projects/':
            if request.method == 'POST':
                return httpx.Response(200, json={'id': 1, **json.loads(request.content)})
            return httpx.Response(200, json=[{'id': 1, 'name': 'Alpha'}])
        return httpx.Response(404, json={'detail': 'Not Found'})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture(scope='module')
def backend():
    return Backend()


@pytest.fixture(scope='module')
def base_url(backend):
    settings = config.Settings(
        portal_url='http://portal.internal', shared_secret=SECRET, signing_key_pem=PRIVATE_PEM,
        token_issuer=ISSUER, allowed_hosts=('gateway.example.ts.net:*',), apps=APPS,
    )
    app = server.build(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(backend)))
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f'http://127.0.0.1:{port}'
    srv.should_exit = True
    thread.join(timeout=5)


def mcp_client(base_url, key=KEY):
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    return Client(streamable_http_client(f'{base_url}/mcp', http_client=httpx2.AsyncClient(headers=headers)))


def payload(result):
    assert not result.is_error, result.content
    return result.structured_content or json.loads(result.content[0].text)


async def test_tools_are_listed(base_url):
    async with mcp_client(base_url) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {'list_apps', 'list_endpoints', 'describe_endpoint', 'call_api'}


async def test_list_apps_shows_only_portal_granted_apps(base_url):
    async with mcp_client(base_url) as client:
        body = payload(await client.call_tool('list_apps', {}))
    assert body['caller'] == 'alice@example.com'
    assert [a['app'] for a in body['apps']] == ['thread']


async def test_call_api_carries_a_fleet_token_for_that_app(base_url, backend):
    async with mcp_client(base_url) as client:
        body = payload(await client.call_tool('call_api', {'app': 'thread', 'method': 'GET', 'path': '/api/v1/projects/'}))
    assert body == {'status': 200, 'body': [{'id': 1, 'name': 'Alpha'}]}

    upstream = backend.calls[-1]
    assert str(upstream.url) == 'http://thread.internal/api/v1/projects/'
    assert upstream.headers['host'] == 'thread.example.com'
    assert 'authorization' not in upstream.headers  # the API key never leaves the gateway
    public_key = _private.public_key()
    claims = jwt.decode(upstream.headers[HEADER], public_key, algorithms=['RS256'],
                        audience='thread.example.com', issuer=ISSUER)
    assert claims['email'] == 'alice@example.com'
    assert claims['exp'] - claims['iat'] <= 60


async def test_writes_are_allowed(base_url, backend):
    async with mcp_client(base_url) as client:
        body = payload(await client.call_tool('call_api', {
            'app': 'thread', 'method': 'POST', 'path': '/api/v1/projects/', 'body': {'name': 'Beta'},
        }))
    assert body['body'] == {'id': 1, 'name': 'Beta'}
    assert backend.calls[-1].headers['content-type'] == 'application/json'


async def test_redirects_are_reported_not_followed(base_url):
    async with mcp_client(base_url) as client:
        body = payload(await client.call_tool('call_api', {'app': 'thread', 'method': 'GET', 'path': '/api/v1/projects'}))
    assert body['status'] == 307
    assert body['location'].endswith('/api/v1/projects/')


async def test_app_not_granted_is_refused_without_calling_it(base_url, backend):
    before = len(backend.calls)
    async with mcp_client(base_url) as client:
        result = await client.call_tool('call_api', {'app': 'estate', 'method': 'GET', 'path': '/api/v1/'})
    assert result.is_error
    assert 'Available: thread' in result.content[0].text
    assert not any(c.url.host == 'estate.internal' for c in backend.calls[before:])


@pytest.mark.parametrize('path', ['http://evil.example.com/x', '//evil.example.com/x', 'api/v1/', '/a\\b'])
async def test_paths_cannot_leave_the_app(base_url, path):
    async with mcp_client(base_url) as client:
        result = await client.call_tool('call_api', {'app': 'thread', 'method': 'GET', 'path': path})
    assert result.is_error


async def test_endpoints_and_descriptions_come_from_openapi(base_url):
    async with mcp_client(base_url) as client:
        listed = payload(await client.call_tool('list_endpoints', {'app': 'thread', 'search': 'project'}))
        described = payload(await client.call_tool('describe_endpoint', {
            'app': 'thread', 'method': 'post', 'path': '/api/v1/projects/',
        }))
    assert [(o['method'], o['path']) for o in listed['operations']] == [
        ('GET', '/api/v1/projects/'), ('POST', '/api/v1/projects/')]
    schema = described['request_body']['content']['application/json']['schema']
    assert schema['properties']['name'] == {'type': 'string'}  # $ref inlined
    assert list(described['responses']) == ['200']


async def test_missing_or_wrong_key_is_refused(base_url):
    async with httpx.AsyncClient() as http:
        no_key = await http.post(f'{base_url}/mcp', json={})
        wrong = await http.post(f'{base_url}/mcp', json={}, headers={'Authorization': 'Bearer afk_nope'})
    assert no_key.status_code == 401 and no_key.headers['www-authenticate'] == 'Bearer'
    assert wrong.status_code == 401


async def test_portal_outage_fails_closed(base_url, backend):
    backend.portal_up = False
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post(f'{base_url}/mcp', json={}, headers={'Authorization': 'Bearer afk_other'})
        assert resp.status_code == 503
    finally:
        backend.portal_up = True


async def test_health_and_jwks_are_public(base_url):
    async with httpx.AsyncClient() as http:
        assert (await http.get(f'{base_url}/healthz')).text == 'ok'
        keys = (await http.get(f'{base_url}/.well-known/jwks.json')).json()['keys']
    assert keys[0]['alg'] == 'RS256' and keys[0]['kid']


async def test_unlisted_host_header_is_refused(base_url):
    # Even with a valid key: only the configured tailnet name (and in-cluster names) are answered.
    async with httpx.AsyncClient() as http:
        resp = await http.post(f'{base_url}/mcp', json={}, headers={
            'Authorization': f'Bearer {KEY}', 'Host': 'mcp.example.com',
            'Accept': 'application/json, text/event-stream'})
    assert resp.status_code == 421
