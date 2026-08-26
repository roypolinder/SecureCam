"""Provider-independent AI interface. Swapping providers must not touch the rest of the system."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..config import AIConfig

PROMPT = (
    "You are a security camera analyst. Look at the image and decide whether a human being is "
    "present. Ignore animals, vehicles, shadows, insects, rain and moving vegetation. "
    'Answer with JSON only, exactly: {"person_detected": true|false, "confidence": 0.0-1.0, '
    '"label": "short label", "summary": "one short sentence"}'
)


class AIError(Exception):
    """Raised when analysis could not be completed."""

    def __init__(self, message: str, retryable: bool = True, hint: str = "") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.hint = hint


@dataclass
class AIResult:
    person_detected: Optional[bool] = None
    confidence: Optional[float] = None
    label: str = ""
    summary: str = ""
    provider: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


class AIProvider:
    """Analyzes one or more JPEG snapshots and reports whether a person is present."""

    name = "base"

    def analyze(self, images: List[bytes], context: Optional[Dict[str, Any]] = None) -> AIResult:
        raise NotImplementedError

    def check(self) -> Tuple[bool, str]:
        """Validate configuration without calling the remote service."""
        return True, ""

    @property
    def enabled(self) -> bool:
        return True


def create_provider(config: AIConfig) -> AIProvider:
    """Instantiate the configured provider."""
    from .disabled import DisabledAI

    if not config.enabled or config.provider == "disabled":
        return DisabledAI()
    if config.provider == "openai_vision":
        from .openai_vision import OpenAIVisionProvider

        return OpenAIVisionProvider(config)
    if config.provider == "generic_http":
        from .generic_http import GenericHTTPProvider

        return GenericHTTPProvider(config)
    return DisabledAI()


def parse_json_answer(text: str) -> Dict[str, Any]:
    """Extract a JSON object from a model reply that may be wrapped in prose or fences."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    brace = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if brace:
        try:
            parsed = json.loads(brace.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise AIError(f"the model did not return JSON: {text[:200]!r}", retryable=False)


def coerce_bool(value: Any) -> Optional[bool]:
    """Interpret the many ways a provider can say yes or no."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1", "person", "human", "detected"):
            return True
        if lowered in ("false", "no", "0", "none", "empty", "nothing"):
            return False
    return None


def coerce_confidence(value: Any) -> Optional[float]:
    """Normalize a confidence into 0.0-1.0, accepting percentages."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))
