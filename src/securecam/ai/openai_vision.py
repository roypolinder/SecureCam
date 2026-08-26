"""OpenAI-compatible vision chat provider (works with OpenAI, OpenRouter, Ollama, vLLM, ...)."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional, Tuple

from ..config import AIConfig
from ..device import get_secret
from ..logging_setup import get_logger
from ..util import HttpError, http_request
from .base import PROMPT, AIError, AIProvider, AIResult, coerce_bool, coerce_confidence, parse_json_answer

log = get_logger("ai.openai_vision")


class OpenAIVisionProvider(AIProvider):
    """Sends snapshots to a chat/completions endpoint that accepts image content."""

    name = "openai_vision"

    def __init__(self, config: AIConfig) -> None:
        self._config = config
        self._settings = config.openai_vision

    def check(self) -> Tuple[bool, str]:
        """Verify the endpoint and key are configured before any event happens."""
        if not self._settings.base_url:
            return False, "ai.openai_vision.base_url is empty"
        if not get_secret(self._settings.api_key_env):
            return False, (
                f"{self._settings.api_key_env} is not set in /etc/securecam/securecam.env, so the AI "
                "endpoint will reject every request"
            )
        return True, ""

    def analyze(self, images: List[bytes], context: Optional[Dict[str, Any]] = None) -> AIResult:
        """Ask the model whether a person is visible and normalize its answer."""
        if not images:
            raise AIError("no snapshot was available to analyze", retryable=False)

        api_key = get_secret(self._settings.api_key_env)
        if not api_key:
            raise AIError(
                f"{self._settings.api_key_env} is not set",
                retryable=False,
                hint="Add the key to /etc/securecam/securecam.env and restart securecam.",
            )

        content: List[Dict[str, Any]] = [{"type": "text", "text": self._prompt(context)}]
        for image in images:
            encoded = base64.b64encode(image).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": self._settings.detail},
                }
            )

        payload = {
            "model": self._settings.model,
            "max_tokens": self._settings.max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "You reply with a single JSON object and nothing else."},
                {"role": "user", "content": content},
            ],
        }

        url = f"{self._settings.base_url}/chat/completions"
        try:
            response = http_request(
                url,
                method="POST",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                data=json.dumps(payload).encode("utf-8"),
                timeout=self._config.timeout_seconds,
            )
        except HttpError as exc:
            raise AIError(str(exc), retryable=exc.retryable, hint=_hint_for(exc)) from exc

        try:
            body = response.json()
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AIError(f"unexpected response shape from {self._settings.model}: {exc}", retryable=False) from exc

        if isinstance(text, list):  # some servers return content parts instead of a string
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))

        answer = parse_json_answer(str(text))
        return AIResult(
            person_detected=coerce_bool(answer.get("person_detected")),
            confidence=coerce_confidence(answer.get("confidence")),
            label=str(answer.get("label", ""))[:80],
            summary=str(answer.get("summary", ""))[:300],
            provider=self.name,
            raw={"model": self._settings.model, "answer": answer},
        )

    def _prompt(self, context: Optional[Dict[str, Any]]) -> str:
        """Prompt text, optionally naming the camera for context."""
        if context and context.get("device_name"):
            return f"Camera location: {context['device_name']}.\n{PROMPT}"
        return PROMPT


def _hint_for(exc: HttpError) -> str:
    """Turn an HTTP status into advice the user can act on."""
    if exc.status == 401:
        return "The API key was rejected. Check SECURECAM_AI_API_KEY in /etc/securecam/securecam.env."
    if exc.status == 404:
        return "The endpoint or model was not found. Check ai.openai_vision.base_url and .model."
    if exc.status == 429:
        return "The provider is rate limiting. SecureCam retries automatically; consider a cheaper model."
    if exc.status is None:
        return "The endpoint was unreachable. Retried automatically once connectivity returns."
    return ""
