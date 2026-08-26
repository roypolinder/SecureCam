"""Structured logging that reads well in `journalctl` and never prints secrets."""

from __future__ import annotations

import contextvars
import logging
import logging.handlers
import os
import sys
from typing import Any, Dict, Iterable, Optional

from .util import ensure_dir, utcnow

LOG_ROOT = "securecam"

_current_event_id: contextvars.ContextVar[str] = contextvars.ContextVar("securecam_event_id", default="-")
_device_id = "-"
_secrets: set = set()
_configured = False


def set_device_id(device_id: str) -> None:
    """Stamp every subsequent log line with this device id."""
    global _device_id
    _device_id = device_id or "-"


def set_event_id(event_id: Optional[str]) -> contextvars.Token:
    """Bind an event id to the current thread/task so related lines can be grepped."""
    return _current_event_id.set(event_id or "-")


def reset_event_id(token: contextvars.Token) -> None:
    """Undo a previous set_event_id."""
    _current_event_id.reset(token)


def register_secret(value: Optional[str]) -> None:
    """Mark a string as sensitive so it is masked if it ever reaches a log line."""
    if value and len(value) >= 6:
        _secrets.add(value)


def register_secrets(values: Iterable[Optional[str]]) -> None:
    """Register several secrets at once."""
    for value in values:
        register_secret(value)


def redact(text: str) -> str:
    """Replace every registered secret inside a string with a fixed mask."""
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, "***redacted***")
    return text


class _ContextFilter(logging.Filter):
    """Attaches device id, event id and a short component name to each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.device_id = _device_id
        record.event_id = _current_event_id.get()
        name = record.name
        record.component = name[len(LOG_ROOT) + 1 :] if name.startswith(LOG_ROOT + ".") else name
        return True


class _TextFormatter(logging.Formatter):
    """Human-readable single-line format."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        message = redact(record.getMessage())
        fields = _format_fields(getattr(record, "fields", None))
        event = getattr(record, "event_id", "-")
        event_part = f" event={event}" if event and event != "-" else ""
        line = (
            f"{stamp} {record.levelname:<8} [{getattr(record, 'component', record.name)}] "
            f"device={getattr(record, 'device_id', '-')}{event_part} {message}{fields}"
        )
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, for shipping logs elsewhere."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        payload: Dict[str, Any] = {
            "timestamp": utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "device_id": getattr(record, "device_id", "-"),
            "event_id": getattr(record, "event_id", "-"),
            "message": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload["fields"] = {key: redact(str(value)) for key, value in extra.items()}
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def _format_fields(fields: Any) -> str:
    """Render an extra fields dict as ' key=value' pairs."""
    if not isinstance(fields, dict) or not fields:
        return ""
    parts = []
    for key, value in fields.items():
        text = redact(str(value))
        if " " in text:
            text = f'"{text}"'
        parts.append(f"{key}={text}")
    return " " + " ".join(parts)


def configure_logging(
    level: str = "INFO",
    fmt: str = "text",
    file_path: str = "",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    """Install SecureCam's handlers on the root logger. Safe to call twice."""
    global _configured
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    resolved = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(resolved)

    formatter: logging.Formatter = _JsonFormatter() if str(fmt).lower() == "json" else _TextFormatter()
    context_filter = _ContextFilter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.addFilter(context_filter)
    root.addHandler(stream)

    if file_path:
        try:
            ensure_dir(os.path.dirname(os.path.abspath(file_path)))
            file_handler = logging.handlers.RotatingFileHandler(
                file_path, maxBytes=int(max_bytes), backupCount=int(backup_count), encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(context_filter)
            root.addHandler(file_handler)
        except OSError as exc:
            root.addHandler(stream)
            logging.getLogger(LOG_ROOT + ".logging").error(
                "Cannot write the log file %s (%s). Continuing with journal logging only. "
                "Fix the path or its permissions, or set logging.file to an empty string.",
                file_path,
                exc,
            )

    # Third-party chatter would drown out our own lines.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _configured = True


def get_logger(component: str) -> logging.Logger:
    """Return the logger for a component, configuring defaults if needed."""
    if not _configured:
        configure_logging()
    return logging.getLogger(f"{LOG_ROOT}.{component}")
