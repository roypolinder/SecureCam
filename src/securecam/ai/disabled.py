"""No-op AI provider used when ai.enabled is false."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .base import AIProvider, AIResult


class DisabledAI(AIProvider):
    """Reports that no analysis was performed, without touching the network."""

    name = "disabled"

    @property
    def enabled(self) -> bool:
        return False

    def analyze(self, images: List[bytes], context: Optional[Dict[str, Any]] = None) -> AIResult:
        return AIResult(provider=self.name, summary="AI analysis is disabled")

    def check(self) -> Tuple[bool, str]:
        return True, "disabled"
