"""ntfy provider.

Priority 5 ("max"/"urgent") is what makes this an alarm rather than a badge: the
ntfy Android app can be told to bypass Do Not Disturb and use a continuous alarm
sound for a topic, and iOS shows it as a time-sensitive notification. Set
notifications.ntfy.call to have ntfy phone you as well.
"""

from __future__ import annotations

import urllib.parse
from typing import Dict, Tuple

from ..config import NotificationsConfig, RecipientConfig
from ..device import get_secret
from ..util import HttpError, http_request
from .base import Notification, NotificationError, NotificationProvider, ascii_header


class NtfyProvider(NotificationProvider):
    """Publishes to an ntfy topic, attaching the snapshot as the request body."""

    name = "ntfy"

    def __init__(self, config: NotificationsConfig) -> None:
        self._config = config
        self._settings = config.ntfy

    def check(self) -> Tuple[bool, str]:
        if not self._settings.server:
            return False, "notifications.ntfy.server is empty"
        if self._settings.server.startswith("http://") and "127.0.0.1" not in self._settings.server:
            return False, (
                "notifications.ntfy.server uses plain HTTP, so the topic name and snapshots travel "
                "unencrypted. Use https:// unless the server is on this machine."
            )
        return True, ""

    def send(self, notification: Notification, recipient: RecipientConfig) -> None:
        """Deliver one notification. Raises NotificationError so the caller can retry."""
        topic = recipient.target.strip().strip("/")
        if not topic:
            raise NotificationError(f"recipient '{recipient.id}' has no ntfy topic", retryable=False)

        alarm = notification.alarm and recipient.alarm and self._config.alarm
        headers: Dict[str, str] = {
            "Title": ascii_header(notification.title),
            "Priority": str(self._settings.priority if alarm else 3),
            "Tags": ascii_header(",".join(notification.tags or self._settings.tags)),
        }
        token = get_secret(self._settings.token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if notification.link:
            headers["Click"] = ascii_header(notification.link)
        if alarm and getattr(self._settings, "call", ""):
            headers["Call"] = ascii_header(self._settings.call)

        if notification.snapshot:
            headers["Message"] = ascii_header(notification.message)
            headers["Filename"] = f"{notification.event_id or 'snapshot'}.jpg"
            headers["Content-Type"] = "image/jpeg"
            body = notification.snapshot
        else:
            headers["Content-Type"] = "text/plain; charset=utf-8"
            body = notification.message.encode("utf-8")

        url = f"{self._settings.server}/{urllib.parse.quote(topic)}"
        try:
            http_request(url, method="POST", headers=headers, data=body, timeout=20.0)
        except HttpError as exc:
            raise NotificationError(str(exc), retryable=exc.retryable, hint=_hint_for(exc)) from exc


def _hint_for(exc: HttpError) -> str:
    """Explain the common ntfy failures."""
    if exc.status in (401, 403):
        return (
            "ntfy rejected the credentials. Protected topics need a token in "
            "SECURECAM_NTFY_TOKEN (see /etc/securecam/securecam.env)."
        )
    if exc.status == 413:
        return "The snapshot was too large for the server. Set notifications.include_snapshot to false."
    if exc.status == 429:
        return "ntfy.sh rate limits anonymous publishing. Use an account, or self-host ntfy."
    if exc.status is None:
        return "The ntfy server was unreachable. The notification stays queued and is retried automatically."
    return ""
