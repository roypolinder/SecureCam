"""Arm/disarm switch. Disarmed means motion is still read but never recorded."""

from __future__ import annotations

import os
import threading
from typing import Any, Dict

from .logging_setup import get_logger
from .util import atomic_write_json, ensure_dir, read_json, to_rfc3339, utcnow

log = get_logger("arming")


class ArmingState:
    """The armed flag, persisted so a restart or power cut does not silently re-arm."""

    def __init__(self, path: str, default_armed: bool = True) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._armed = bool(default_armed)
        self._changed_at = to_rfc3339(utcnow())
        self._changed_by = "default"
        self._load()

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed

    def set(self, armed: bool, actor: str = "unknown") -> bool:
        """Change the state and persist it. True when it actually changed."""
        armed = bool(armed)
        with self._lock:
            if armed == self._armed:
                return False
            self._armed = armed
            self._changed_at = to_rfc3339(utcnow())
            self._changed_by = actor or "unknown"
            snapshot = self._snapshot()
        self._save(snapshot)
        if armed:
            log.warning("Camera armed by %s; motion will be recorded again", snapshot["changed_by"])
        else:
            log.warning(
                "Camera disarmed by %s. Live view keeps working and the rolling buffer keeps running, "
                "but motion will not create events until it is armed again.",
                snapshot["changed_by"],
            )
        return True

    def status(self) -> Dict[str, Any]:
        """Serializable state for the API and health report."""
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> Dict[str, Any]:
        return {"armed": self._armed, "changed_at": self._changed_at, "changed_by": self._changed_by}

    def _load(self) -> None:
        try:
            payload = read_json(self._path)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning(
                "Could not read the armed state from %s (%s); falling back to the configured default (%s)",
                self._path,
                exc,
                "armed" if self._armed else "disarmed",
            )
            return
        if not isinstance(payload, dict) or "armed" not in payload:
            return
        self._armed = bool(payload["armed"])
        self._changed_at = str(payload.get("changed_at") or self._changed_at)
        self._changed_by = str(payload.get("changed_by") or "unknown")

    def _save(self, snapshot: Dict[str, Any]) -> None:
        try:
            ensure_dir(os.path.dirname(os.path.abspath(self._path)))
            atomic_write_json(self._path, snapshot, mode=0o640)
        except OSError as exc:
            log.error(
                "Could not save the armed state to %s: %s\n"
                "  What still works: the change took effect immediately, but a restart will forget it.\n"
                "  Fix: make sure the service user can write that directory, then set it again.",
                self._path,
                exc,
            )
