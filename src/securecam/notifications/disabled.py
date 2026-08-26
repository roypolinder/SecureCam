"""No-op notifier used when notifications are switched off."""

from __future__ import annotations

from typing import Tuple

from ..config import RecipientConfig
from .base import Notification, NotificationProvider


class DisabledNotifier(NotificationProvider):
    """Accepts notifications and discards them."""

    name = "disabled"

    @property
    def enabled(self) -> bool:
        return False

    def send(self, notification: Notification, recipient: RecipientConfig) -> None:
        return None

    def check(self) -> Tuple[bool, str]:
        return True, "disabled"
