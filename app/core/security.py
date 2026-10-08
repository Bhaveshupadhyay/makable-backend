"""Cryptographic primitives: random tokens, PKCE, signing and encryption."""

import base64
import hashlib
import secrets
from typing import Any

from cryptography.fernet import Fernet
from itsdangerous import BadSignature, URLSafeTimedSerializer


class InvalidTokenError(Exception):
    """A signed value or JWT is malformed, tampered with or expired."""


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def pkce_challenge(verifier: str) -> str:
    """The S256 PKCE code challenge for `verifier`."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class PayloadSigner:
    """Signs small payloads (e.g. OAuth state) so they can round-trip through the browser untampered."""

    def __init__(self, secret_key: str, *, salt: str) -> None:
        self._serializer = URLSafeTimedSerializer(secret_key, salt=salt)

    def dumps(self, payload: dict[str, Any]) -> str:
        return self._serializer.dumps(payload)

    def loads(self, value: str, *, max_age: int) -> dict[str, Any]:
        """Verifies and decodes a signed payload.

        Raises:
            InvalidTokenError: The signature is wrong or older than `max_age` seconds.
        """
        try:
            payload = self._serializer.loads(value, max_age=max_age)
        except BadSignature as err:  # SignatureExpired is a BadSignature.
            raise InvalidTokenError(str(err)) from err
        if not isinstance(payload, dict):
            raise InvalidTokenError("payload is not an object")
        return payload


class TokenCipher:
    """Symmetric encryption for secrets kept at rest (GitHub tokens)."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()
