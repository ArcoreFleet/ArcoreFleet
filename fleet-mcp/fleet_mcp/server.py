"""The Fleet MCP gateway: one API key, every ArcoreFleet app the key's owner can open.
Reachable only over the tailnet (k3s/fleet-mcp.yaml).

    uvicorn fleet_mcp.server:app --host 0.0.0.0 --port 8000 --proxy-headers

Endpoints: /mcp (MCP, streamable HTTP, needs the key), /healthz, and
/.well-known/jwks.json (the public key apps verify fleet tokens with).
"""

import logging
from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from . import config
from .auth import BearerKeyMiddleware, Caller, KeyResolver
from .gateway import Gateway, GatewayError, describe_operation, list_operations
from .signing import Signer

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')

INSTRUCTIONS = """\
Gateway to the ArcoreFleet apps (ArcoreThread, ArcoreEstate, ...). Every call acts as the API
key's owner, with exactly their access: what they could do in the browser, nothing more.
Start with list_apps, then list_endpoints / describe_endpoint to find the right call, then
call_api. Writes (POST/PUT/PATCH/DELETE) change real records immediately."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def build(settings: config.Settings, client: httpx.AsyncClient | None = None):
    client = client or httpx.AsyncClient()
    signer = Signer(settings.signing_key_pem, settings.token_issuer)
    gateway = Gateway(settings.apps, signer, client)
    resolver = KeyResolver(settings.portal_url, settings.shared_secret, client)
    mcp = MCPServer(name='arcorefleet', title='ArcoreFleet', instructions=INSTRUCTIONS)

    def caller_of(ctx: Context) -> Caller:
        caller = getattr(ctx.request_context.request.state, 'caller', None)
        if caller is None:  # BearerKeyMiddleware guarantees this on /mcp
            raise ToolError('No authenticated caller')
        return caller

    async def run(coro):
        try:
            return await coro
        except GatewayError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations=READ_ONLY)
    def list_apps(ctx: Context) -> dict:
        """The apps this API key can use, with notes on each app's API conventions."""
        caller = caller_of(ctx)
        return {
            'caller': caller.email,
            'is_admin': caller.is_admin,
            'apps': [{'app': a.id, 'name': a.name, 'hostname': a.hostname, 'notes': a.notes}
                     for a in gateway.available(caller)],
        }

    @mcp.tool(annotations=READ_ONLY)
    async def list_endpoints(app: str, ctx: Context, search: str | None = None) -> dict:
        """List an app's API operations (method, path, summary) from its OpenAPI schema.

        Args:
            app: The app id from list_apps, e.g. "thread".
            search: Optional text to filter by, matched against path, summary and tags.
        """
        caller = caller_of(ctx)
        schema = await run(gateway.schema(caller, app))
        ops = list_operations(schema, search)
        return {'app': app, 'count': len(ops), 'operations': ops}

    @mcp.tool(annotations=READ_ONLY)
    async def describe_endpoint(app: str, method: str, path: str, ctx: Context) -> dict:
        """Parameters, request body schema and success responses of one operation.

        Args:
            app: The app id from list_apps.
            method: HTTP method, e.g. "POST".
            path: The path exactly as list_endpoints shows it, e.g. "/api/v1/projects/{project_id}".
        """
        caller = caller_of(ctx)
        schema = await run(gateway.schema(caller, app))
        try:
            return describe_operation(schema, method, path)
        except GatewayError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    async def call_api(
        app: str, method: str, path: str, ctx: Context,
        query: dict[str, Any] | None = None, body: Any = None,
    ) -> dict:
        """Call an app's API as the key's owner and return the status and JSON body.

        Reads (GET) and writes (POST, PUT, PATCH, DELETE) are both allowed; writes take effect
        immediately. Fill path parameters in yourself (/api/v1/projects/3, not {project_id}).

        Args:
            app: The app id from list_apps.
            method: GET, POST, PUT, PATCH or DELETE.
            path: Absolute path on the app, including any trailing slash, e.g. "/api/v1/projects/".
            query: Optional query-string parameters.
            body: Optional JSON request body.
        """
        caller = caller_of(ctx)
        return await run(gateway.request(caller, app, method, path, query, body))

    @mcp.custom_route('/healthz', methods=['GET'])
    async def healthz(request: Request):
        return PlainTextResponse('ok')

    @mcp.custom_route('/.well-known/jwks.json', methods=['GET'])
    async def jwks(request: Request):
        return JSONResponse(signer.jwks(), headers={'Cache-Control': 'public, max-age=300'})

    starlette_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[*settings.allowed_hosts, 'fleet-mcp.apps.svc.cluster.local', 'localhost:*', '127.0.0.1:*'],
        ),
    )
    return BearerKeyMiddleware(starlette_app, resolver)


def __getattr__(name: str):
    # `uvicorn fleet_mcp.server:app` builds from the environment; tests call build() directly.
    if name == 'app':
        return build(config.from_env())
    raise AttributeError(name)
