"""Configuration from the environment (the fleet-mcp-env Secret and the manifest)."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_APPS_FILE = Path(__file__).with_name('apps.json')


@dataclass(frozen=True)
class App:
    """An app the gateway can reach. Which callers may use it is the portal's decision."""

    id: str
    name: str
    hostname: str
    upstream: str
    schema: str
    notes: str = ''


@dataclass(frozen=True)
class Settings:
    portal_url: str
    shared_secret: str
    signing_key_pem: str
    token_issuer: str
    allowed_hosts: tuple[str, ...]
    apps: tuple[App, ...]


def load_apps(path: Path) -> tuple[App, ...]:
    data = json.loads(path.read_text())
    apps = tuple(App(**{**a, 'upstream': a['upstream'].rstrip('/')}) for a in data['apps'])
    ids = [a.id for a in apps]
    if len(ids) != len(set(ids)):
        raise ValueError(f'{path}: app ids must be unique')
    return apps


def from_env() -> Settings:
    missing = [n for n in ('FLEET_MCP_SHARED_SECRET', 'FLEET_SIGNING_KEY') if not os.getenv(n)]
    if missing:
        # Fail closed: without these the gateway can neither check keys nor speak for anyone.
        raise RuntimeError(f'Set {", ".join(missing)}')
    return Settings(
        portal_url=os.getenv('PORTAL_URL', 'http://portal.apps.svc.cluster.local').rstrip('/'),
        shared_secret=os.environ['FLEET_MCP_SHARED_SECRET'],
        signing_key_pem=os.environ['FLEET_SIGNING_KEY'],
        token_issuer=os.getenv('FLEET_TOKEN_ISSUER', 'https://mcp.example.com'),
        # Host headers the MCP endpoint answers to (DNS-rebinding protection); "name:*" allows any port.
        allowed_hosts=tuple(h.strip() for h in os.getenv('FLEET_ALLOWED_HOSTS', 'localhost:*').split(',') if h.strip()),
        apps=load_apps(Path(os.getenv('FLEET_APPS_FILE', str(DEFAULT_APPS_FILE)))),
    )
