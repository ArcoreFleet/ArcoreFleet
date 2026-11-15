"""Fleet identity tokens: how the gateway tells an app who it is acting for.

Each upstream call carries a short-lived RS256 JWT in X-Arcore-Fleet-Token:
the caller's email, audience = the app's hostname (so a token for one app is
refused by every other), issuer = this gateway. Apps verify it against
/.well-known/jwks.json, exactly as they verify Cloudflare Access tokens.
"""

import base64
import hashlib
import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from jwt.algorithms import RSAAlgorithm

HEADER = 'X-Arcore-Fleet-Token'
LIFETIME_SECONDS = 60


class Signer:
    def __init__(self, private_key_pem: str, issuer: str):
        self._key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        self.issuer = issuer
        public_der = self._key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.kid = base64.urlsafe_b64encode(hashlib.sha256(public_der).digest()[:12]).decode().rstrip('=')

    def sign(self, email: str, audience: str) -> str:
        now = int(time.time())
        claims = {
            'iss': self.issuer, 'aud': audience, 'sub': email, 'email': email,
            'iat': now, 'exp': now + LIFETIME_SECONDS, 'jti': uuid.uuid4().hex,
        }
        return jwt.encode(claims, self._key, algorithm='RS256', headers={'kid': self.kid})

    def jwks(self) -> dict:
        jwk = RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        return {'keys': [{**jwk, 'kid': self.kid, 'use': 'sig', 'alg': 'RS256'}]}
