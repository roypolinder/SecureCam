"""Provider-independent notification interface.

`alarm` is a first-class concept here: a security notification is only useful if
it can wake somebody up, so providers are expected to map it onto their loudest
delivery mode (ntfy max/urgent priority, Pushover emergency priority).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..config import NotificationsConfig, RecipientConfig


class NotificationError(Exception):
    """Raised when a notification could not be delivered."""

    def __init__(self, message: str, retryable: bool = True, hint: str = "") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.hint = hint


@dataclass
class Notification:
    title: str
    message: str
    event_id: str = ""
    device_name: str = ""
    alarm: bool = True
    link: str = ""
    snapshot: Optional[bytes] = None
    tags: List[str] = field(default_factory=list)


@dataclass
class DeliveryResult:
    recipient_id: str
    provider: str
    ok: bool
    error: str = ""
    detail: Dict[str, str] = field(default_factory=dict)


class NotificationProvider:
    """Delivers a notification to one recipient."""

    name = "base"

    def send(self, notification: Notification, recipient: RecipientConfig) -> None:
        raise NotImplementedError

    def check(self) -> Tuple[bool, str]:
        """Validate configuration without contacting the service."""
        return True, ""

    @property
    def enabled(self) -> bool:
        return True


def create_provider(config: NotificationsConfig, name: str = "") -> NotificationProvider:
    """Instantiate a provider by name, falling back to the global setting."""
    from .disabled import DisabledNotifier

    chosen = name or config.provider
    if not config.enabled or chosen in ("", "disabled"):
        return DisabledNotifier()
    if chosen == "ntfy":
        from .ntfy import NtfyProvider

        return NtfyProvider(config)
    if chosen == "pushover":
        from .pushover import PushoverProvider

        return PushoverProvider(config)
    return DisabledNotifier()


def ascii_header(value: str) -> str:
    """HTTP headers must be latin-1; drop anything that cannot be encoded."""
    return value.encode("ascii", errors="replace").decode("ascii").replace("\n", " ").replace("\r", " ")
