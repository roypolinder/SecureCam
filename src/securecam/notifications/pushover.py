"""Pushover provider.

Priority 2 is Pushover's emergency level: the phone repeats the alert every
`retry_seconds` until somebody acknowledges it, and it ignores the user's quiet
hours. That is the closest thing to a real alarm any push service offers.
"""

from __future__ import annotations

from typing import Dict, Tuple

from ..config import NotificationsConfig, RecipientConfig
from ..device import get_secret
from ..util import HttpError, encode_multipart, http_request
from .base import Notification, NotificationError, NotificationProvider


class PushoverProvider(NotificationProvider):
    """Sends to the Pushover messages API, attaching the snapshot when there is one."""

    name = "pushover"

    def __init__(self, config: NotificationsConfig) -> None:
        self._config = config
        self._settings = config.pushover

    def check(self) -> Tuple[bool, str]:
        if not get_secret(self._settings.token_env):
            return False, (
                f"{self._settings.token_env} is not set in /etc/securecam/securecam.env. "
                "Create an application at https://pushover.net/apps/build to get one."
            )
        if self._settings.priority == 2 and self._settings.expire_seconds < self._settings.retry_seconds:
            return False, "notifications.pushover.expire_seconds must be >= retry_seconds for emergency priority"
        return True, ""

    def send(self, notification: Notification, recipient: RecipientConfig) -> None:
        """Deliver one notification. Raises NotificationError so the caller can retry."""
        token = get_secret(self._settings.token_env)
        if not token:
            raise NotificationError(
                f"{self._settings.token_env} is not set",
                retryable=False,
                hint="Add the Pushover application token to /etc/securecam/securecam.env.",
            )
        if not recipient.target:
            raise NotificationError(f"recipient '{recipient.id}' has no Pushover user key", retryable=False)

        alarm = notification.alarm and recipient.alarm and self._config.alarm
        priority = self._settings.priority if alarm else 0
        fields: Dict[str, str] = {
            "token": token,
            "user": recipient.target,
            "title": notification.title,
            "message": notification.message,
            "priority": str(priority),
            "sound": self._settings.sound if alarm else "pushover",
        }
        if priority == 2:
            fields["retry"] = str(self._settings.retry_seconds)
            fields["expire"] = str(self._settings.expire_seconds)
        if notification.link:
            fields["url"] = notification.link
            fields["url_title"] = "Open camera"

        files = []
        if notification.snapshot:
            files.append(("attachment", f"{notification.event_id or 'snapshot'}.jpg", notification.snapshot))

        body, content_type = encode_multipart(fields, files)
        try:
            response = http_request(
                self._settings.api_url,
                method="POST",
                headers={"Content-Type": content_type},
                data=body,
                timeout=30.0,
            )
        except HttpError as exc:
            raise NotificationError(str(exc), retryable=exc.retryable, hint=_hint_for(exc)) from exc

        try:
            payload = response.json()
        except ValueError:
            return
        if payload.get("status") != 1:
            errors = "; ".join(str(item) for item in payload.get("errors", [])) or "unknown error"
            raise NotificationError(f"Pushover rejected the message: {errors}", retryable=False)


def _hint_for(exc: HttpError) -> str:
    """Explain the common Pushover failures."""
    if exc.status == 400:
        return "Pushover rejected a field, usually a wrong user/group key in notifications.recipients[].target."
    if exc.status in (401, 403):
        return "The application token was rejected. Check SECURECAM_PUSHOVER_TOKEN."
    if exc.status == 429:
        return "The monthly Pushover message limit is exhausted."
    if exc.status is None:
        return "Pushover was unreachable. The notification stays queued and is retried automatically."
    return ""
