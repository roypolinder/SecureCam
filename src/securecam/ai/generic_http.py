"""Generic HTTP AI provider for self-hosted detectors that speak plain JSON."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional, Tuple

from ..config import AIConfig
from ..device import get_secret
from ..util import HttpError, dotted_get, encode_multipart, http_request
from .base import AIError, AIProvider, AIResult, coerce_bool, coerce_confidence


class GenericHTTPProvider(AIProvider):
    """POSTs a snapshot to any endpoint and reads the result from configurable JSON fields."""

    name = "generic_http"

    def __init__(self, config: AIConfig) -> None:
        self._config = config
        self._settings = config.generic_http

    def check(self) -> Tuple[bool, str]:
        if not self._settings.endpoint:
            return False, "ai.generic_http.endpoint is empty"
        return True, ""

    def analyze(self, images: List[bytes], context: Optional[Dict[str, Any]] = None) -> AIResult:
        """Send the first snapshot and map the response onto the common result shape."""
        if not images:
            raise AIError("no snapshot was available to analyze", retryable=False)
        if not self._settings.endpoint:
            raise AIError("ai.generic_http.endpoint is not configured", retryable=False)

        headers: Dict[str, str] = {}
        api_key = get_secret(self._settings.api_key_env)
        if api_key and self._settings.auth_header:
            prefix = f"{self._settings.auth_scheme} " if self._settings.auth_scheme else ""
            headers[self._settings.auth_header] = f"{prefix}{api_key}"

        if self._settings.encoding == "json_base64":
            body = json.dumps(
                {
                    self._settings.field_name: base64.b64encode(images[0]).decode("ascii"),
                    "device_name": (context or {}).get("device_name", ""),
                    "event_id": (context or {}).get("event_id", ""),
                }
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body, content_type = encode_multipart(
                {"device_name": (context or {}).get("device_name", "")},
                [(self._settings.field_name, "snapshot.jpg", images[0])],
            )
            headers["Content-Type"] = content_type

        try:
            response = http_request(
                self._settings.endpoint,
                method="POST",
                headers=headers,
                data=body,
                timeout=self._config.timeout_seconds,
            )
        except HttpError as exc:
            raise AIError(str(exc), retryable=exc.retryable) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise AIError(f"the endpoint did not return JSON: {response.text[:200]!r}", retryable=False) from exc

        return AIResult(
            person_detected=coerce_bool(dotted_get(payload, self._settings.person_field)),
            confidence=coerce_confidence(dotted_get(payload, self._settings.confidence_field)),
            label=str(dotted_get(payload, self._settings.label_field, "") or "")[:80],
            summary="",
            provider=self.name,
            raw=payload if isinstance(payload, dict) else {"response": payload},
        )
