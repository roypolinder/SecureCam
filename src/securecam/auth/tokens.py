"""HMAC-signed, stateless tokens for API sessions and short-lived stream tickets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any, Dict, Optional

from ..util import utcnow


class TokenError(Exception):
    """Raised when a token is malformed, forged or expired."""


class TokenSigner:
    """Signs and verifies compact tokens. No server-side session storage is needed."""

    def __init__(self, secret: bytes) -> None:
        if not secret:
            raise ValueError("token secret must not be empty")
        self._secret = secret

    def issue(self, payload: Dict[str, Any], ttl_seconds: float) -> str:
        """Create a token that expires after ttl_seconds."""
        body = dict(payload)
        body["iat"] = int(utcnow().timestamp())
        body["exp"] = int(utcnow().timestamp() + ttl_seconds)
        body.setdefault("jti", secrets.token_hex(8))
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        encoded = _b64encode(raw)
        signature = _b64encode(hmac.new(self._secret, encoded, hashlib.sha256).digest())
        return f"{encoded.decode('ascii')}.{signature.decode('ascii')}"

    def verify(self, token: str, expected_kind: Optional[str] = None) -> Dict[str, Any]:
        """Validate signature and expiry, returning the payload."""
        if not token or token.count(".") != 1:
            raise TokenError("malformed token")
        encoded, _, signature = token.partition(".")
        expected = _b64encode(hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(expected.decode("ascii"), signature):
            raise TokenError("invalid token signature")
        try:
            payload = json.loads(_b64decode(encoded.encode("ascii")).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TokenError("token payload is not readable") from exc
        if not isinstance(payload, dict):
            raise TokenError("token payload is not an object")
        if float(payload.get("exp", 0)) < utcnow().timestamp():
            raise TokenError("token has expired")
        if expected_kind is not None and payload.get("kind") != expected_kind:
            raise TokenError(f"token is not a {expected_kind} token")
        return payload


def _b64encode(data: bytes) -> bytes:
    """URL-safe base64 without padding, so tokens are safe in headers and URLs."""
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _b64decode(data: bytes) -> bytes:
    """Reverse of _b64encode."""
    padding = b"=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)
