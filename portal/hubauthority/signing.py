"""Signing for the ArcoreHub authority (no Django imports, so tools and tests can use it).

The chain a Hub verifies (ArcoreHub `crates/trust`):

    root key (offline; public half embedded in the Hub)
      -> Delegation: this portal's configuration key may sign one authority's config
           -> AuthorityConfig: endpoints, TLS pins, version, ...

Signatures cover the exact payload bytes, carried as base64 in the envelope:

    {"payload": "<base64 JSON bytes>", "signatures": [{"key_id": "...", "sig": "<base64>"}]}

so no canonical JSON is needed across languages. The portal holds only the
configuration key (HUB_CONFIG_KEY, a base64 Ed25519 seed) and the root-signed
delegation (HUB_CONFIG_DELEGATION). The root key never reaches the cluster.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

PROFILE_FORMAT = 'arcoreprofile'
PROFILE_VERSION = 1
SCHEMA = 1
DELEGATION_KIND = 'authority-config-delegation'
CONFIG_KIND = 'authority-config'
MAX_ENDPOINTS = 8
MAX_PINS = 8
MAX_DOWNLOADS = 8


class NotConfigured(Exception):
    """The configuration key or delegation is missing, malformed or inconsistent."""


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def key_id(public: bytes) -> str:
    """First 16 hex digits of SHA-256 of the raw public key (same as the Hub)."""
    return hashlib.sha256(public).digest()[:8].hex()


def sign(obj: dict, key: Ed25519PrivateKey) -> dict:
    payload = json.dumps(obj, separators=(',', ':')).encode('utf-8')
    return {
        'payload': b64(payload),
        'signatures': [{'key_id': key_id(public_bytes(key.public_key())), 'sig': b64(key.sign(payload))}],
    }


def verify(signed: dict, public: bytes) -> dict:
    """Verify `signed` with `public` and return the decoded payload."""
    payload = base64.b64decode(signed['payload'])
    kid = key_id(public)
    pub = Ed25519PublicKey.from_public_bytes(public)
    for entry in signed.get('signatures', []):
        if entry.get('key_id') != kid:
            continue
        try:
            pub.verify(base64.b64decode(entry['sig']), payload)
            return json.loads(payload)
        except (InvalidSignature, ValueError):
            continue
    raise InvalidSignature('no valid signature from the expected key')


@dataclass(frozen=True)
class Signer:
    key: Ed25519PrivateKey
    delegation_signed: dict
    delegation: dict

    @property
    def authority_id(self) -> str:
        return self.delegation['authority_id']

    @property
    def environment(self) -> str:
        return self.delegation['environment']

    @property
    def expires(self) -> int:
        return int(self.delegation['expires'])

    @property
    def key_id(self) -> str:
        return self.delegation['key_id']


def load_signer(seed_b64: str, delegation_json: str, now: int | None = None) -> Signer:
    """Load the configuration key and its delegation, checking they belong together.

    The delegation's root signature is not checked here (the portal does not hold
    the root); the Hub checks it against its embedded root.
    """
    now = int(time.time()) if now is None else now
    if not seed_b64 or not delegation_json:
        raise NotConfigured('HUB_CONFIG_KEY and HUB_CONFIG_DELEGATION are not set.')
    try:
        seed = base64.b64decode(seed_b64.strip(), validate=True)
        key = Ed25519PrivateKey.from_private_bytes(seed)
    except ValueError as exc:
        raise NotConfigured('HUB_CONFIG_KEY is not a base64 Ed25519 seed.') from exc
    try:
        signed = json.loads(delegation_json)
        delegation = json.loads(base64.b64decode(signed['payload']))
    except (ValueError, KeyError, TypeError) as exc:
        raise NotConfigured('HUB_CONFIG_DELEGATION is not a signed delegation.') from exc
    if delegation.get('kind') != DELEGATION_KIND or delegation.get('schema') != SCHEMA:
        raise NotConfigured('HUB_CONFIG_DELEGATION is not a configuration-key delegation.')
    if delegation.get('key_id') != key_id(public_bytes(key.public_key())):
        raise NotConfigured('HUB_CONFIG_DELEGATION delegates a different key than HUB_CONFIG_KEY.')
    if delegation.get('public_key') != b64(public_bytes(key.public_key())):
        raise NotConfigured('HUB_CONFIG_DELEGATION public key does not match HUB_CONFIG_KEY.')
    if int(delegation.get('expires', 0)) <= now:
        raise NotConfigured('The configuration-key delegation has expired; ask the root holder for a new one.')
    if delegation.get('environment') not in ('dev', 'prod'):
        raise NotConfigured('The delegation has an unknown environment.')
    return Signer(key=key, delegation_signed=signed, delegation=delegation)


# ---- validation (mirrors the Hub's checks, so bad configs fail here, not in the field)

_DNS_LABEL = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')


# Exactly the ranges the Hub treats as private (crates/trust/src/endpoint.rs). Python's
# own is_private is broader (documentation and reserved ranges), so it is not used.
_PRIVATE = [ipaddress.ip_network(n) for n in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',  # RFC 1918
    '127.0.0.0/8', '169.254.0.0/16', '0.0.0.0/32',  # loopback, link-local, unspecified
    '100.64.0.0/10',  # carrier-grade NAT, which Tailscale uses
    '::1/128', '::/128', 'fc00::/7', 'fe80::/10',  # IPv6 loopback, unspecified, ULA, link-local
)]


def _private(ip: ipaddress._BaseAddress) -> bool:
    return any(ip.version == net.version and ip in net for net in _PRIVATE)


def parse_endpoint(text: str, allow_private: bool) -> dict:
    """`host:port` or `[ipv6]:port` -> {"host", "port"}, validated like the Hub does."""
    text = text.strip()
    m = re.fullmatch(r'\[([0-9a-fA-F:.]+)\]:(\d{1,5})', text) or re.fullmatch(r'([^:\s/@]+):(\d{1,5})', text)
    if not m:
        raise ValueError(f'{text!r}: use host:port or [ipv6]:port')
    host, port = m.group(1), int(m.group(2))
    if not 1 <= port <= 65535:
        raise ValueError(f'{text!r}: port must be 1-65535')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _private(ip) and not allow_private:
            raise ValueError(f'{host} is a private address; allow private endpoints to use it')
        return {'host': str(ip), 'port': port}
    labels = host.split('.')
    if (
        host != host.lower() or len(host) > 253 or len(labels) < 2
        or not all(_DNS_LABEL.match(label) for label in labels)
        or all(label.isdigit() for label in labels)
    ):
        raise ValueError(f'{host!r} is not a valid lowercase DNS name or IP address')
    return {'host': host, 'port': port}


def parse_pin(text: str) -> str:
    text = text.strip()
    if not text.startswith('sha256/'):
        raise ValueError(f'{text!r}: pins look like sha256/<base64>')
    try:
        raw = base64.b64decode(text[len('sha256/'):], validate=True)
    except ValueError as exc:
        raise ValueError(f'{text!r}: not base64') from exc
    if len(raw) != 32:
        raise ValueError(f'{text!r}: must be a 32-byte SHA-256')
    return text


def build_config(
    signer: Signer, *, tenant: str, version: int, endpoints: list[dict], tls_pins: list[str],
    allow_private_endpoints: bool, validity_days: int, download_locations: list[str],
    protocol_min: int = 1, now: int | None = None,
) -> dict:
    now = int(time.time()) if now is None else now
    if not endpoints or len(endpoints) > MAX_ENDPOINTS:
        raise ValueError(f'list 1-{MAX_ENDPOINTS} endpoints')
    if not tls_pins or len(tls_pins) > MAX_PINS:
        raise ValueError(f'list 1-{MAX_PINS} TLS pins')
    if len(download_locations) > MAX_DOWNLOADS or any(not u.startswith('https://') for u in download_locations):
        raise ValueError('download locations must be https:// URLs')
    tenant = tenant.strip()
    if not tenant or len(tenant) > 64:
        raise ValueError('tenant must be 1-64 characters')
    # A configuration never outlives the delegation that authorizes its key.
    expires = min(now + max(1, validity_days) * 86400, signer.expires)
    return {
        'schema': SCHEMA,
        'kind': CONFIG_KIND,
        'environment': signer.environment,
        'authority_id': signer.authority_id,
        'tenant': tenant,
        'version': version,
        'issued_at': now - 60,
        'expires': expires,
        'endpoints': endpoints,
        'tls_pins': tls_pins,
        'allow_private_endpoints': allow_private_endpoints,
        'protocol_min': protocol_min,
        'download_locations': download_locations,
    }


def profile(delegation_signed: dict, config_signed: dict) -> dict:
    return {
        'format': PROFILE_FORMAT,
        'version': PROFILE_VERSION,
        'delegation': delegation_signed,
        'config': config_signed,
    }
