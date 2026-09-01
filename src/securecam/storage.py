"""Disk usage accounting and retention. Deletes oldest events first, never active ones."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional

from .config import Config
from .events import EventStore, EventStatus
from .logging_setup import get_logger
from .util import dir_size_bytes, ensure_dir, human_bytes, parse_rfc3339, utcnow

log = get_logger("storage")


@dataclass
class StorageStatus:
    path: str = ""
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    used_percent: float = 0.0
    events_bytes: int = 0
    buffer_bytes: int = 0
    event_count: int = 0
    writable: bool = True
    over_limit: bool = False
    critical: bool = False
    detail: str = ""
    last_cleanup: Optional[str] = None
    deleted_last_run: int = 0


@dataclass
class CleanupResult:
    deleted_events: int = 0
    freed_bytes: int = 0
    reasons: Dict[str, int] = field(default_factory=dict)
    still_over_limit: bool = False


class StorageManager:
    """Owns the storage tree and enforces retention_days, max_usage_percent and min_free_gb."""

    def __init__(self, config: Config, store: EventStore) -> None:
        self._config = config
        self._store = store
        self._status = StorageStatus(path=config.storage.base_path)
        self._warned_full = False
        self._warned_readonly = False

    def prepare(self) -> None:
        """Create the storage tree and fail loudly if it is not writable."""
        for path in (
            self._config.storage.base_path,
            self._config.storage.events_path,
            self._config.storage.buffer_path,
        ):
            ensure_dir(path)
        probe = os.path.join(self._config.storage.base_path, ".write-test")
        try:
            with open(probe, "wb") as handle:
                handle.write(b"ok")
            os.unlink(probe)
        except OSError as exc:
            raise RuntimeError(
                f"Storage directory {self._config.storage.base_path} is not writable: {exc}\n"
                "  Why it probably failed: the filesystem is mounted read-only after an unclean shutdown,\n"
                "  the disk is full, or the securecam user does not own the directory.\n"
                f"  Diagnose: sudo ./scripts/diagnose-storage.sh\n"
                f"  Fix: sudo chown -R securecam:securecam {self._config.storage.base_path}"
            ) from exc

    def status(self, refresh_sizes: bool = False) -> StorageStatus:
        """Current disk figures. Directory sizes are only walked when asked for."""
        settings = self._config.storage
        status = StorageStatus(path=settings.base_path, last_cleanup=self._status.last_cleanup)
        try:
            usage = shutil.disk_usage(settings.base_path)
        except OSError as exc:
            status.writable = False
            status.detail = f"cannot stat {settings.base_path}: {exc}"
            self._status = status
            return status

        status.total_bytes = usage.total
        status.used_bytes = usage.used
        status.free_bytes = usage.free
        status.used_percent = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
        status.writable = os.access(settings.base_path, os.W_OK)
        status.over_limit = self._over_limit(usage.used, usage.total, usage.free)
        status.critical = usage.free < 200 * 1024 * 1024
        status.deleted_last_run = self._status.deleted_last_run

        if refresh_sizes:
            status.events_bytes = dir_size_bytes(settings.events_path)
            status.buffer_bytes = dir_size_bytes(settings.buffer_path)
            status.event_count = self._store.count()
        else:
            status.events_bytes = self._status.events_bytes
            status.buffer_bytes = self._status.buffer_bytes
            status.event_count = self._status.event_count

        if not status.writable and not self._warned_readonly:
            self._warned_readonly = True
            log.error(
                "%s is not writable. New events cannot be saved; live streaming still works.\n"
                "  Likely cause: the root filesystem was remounted read-only after a filesystem error.\n"
                "  Diagnose: sudo ./scripts/diagnose-storage.sh ; dmesg | tail -30\n"
                "  Fix: reboot, then run sudo fsck on the affected filesystem if the error repeats.",
                settings.base_path,
            )
        elif status.writable:
            self._warned_readonly = False

        self._status = status
        return status

    def enforce(self) -> CleanupResult:
        """Apply retention rules. Safe to call often; it only deletes what it must."""
        result = CleanupResult()
        settings = self._config.storage
        cutoff = utcnow() - timedelta(days=settings.retention_days)

        for event in list(self._store.iter_events(newest_first=False)):
            if event.active:
                continue
            try:
                created = parse_rfc3339(event.created_at or event.started_at)
            except ValueError:
                continue
            if created >= cutoff:
                break  # iteration is oldest-first, so nothing later can be older
            result.freed_bytes += dir_size_bytes(event.directory)
            self._store.delete_directory(event.directory)
            result.deleted_events += 1
            result.reasons["retention_days"] = result.reasons.get("retention_days", 0) + 1
            log.info("Deleted event %s (older than %d days)", event.event_id, settings.retention_days)

        status = self.status()
        if status.over_limit:
            self._free_space(result, status)

        final = self.status(refresh_sizes=True)
        result.still_over_limit = final.over_limit
        self._status.last_cleanup = utcnow().isoformat()
        self._status.deleted_last_run = result.deleted_events

        if result.still_over_limit and not self._warned_full:
            self._warned_full = True
            log.error(
                "Storage is still above the limit after deleting everything that may be deleted "
                "(%s used, %s free, limit %d%%/%.1f GB).\n"
                "  What still works: recording continues until the disk is physically full.\n"
                "  Likely cause: something other than SecureCam is using the disk, or retention_days is\n"
                "  larger than the disk can hold at the configured bitrate.\n"
                "  Fix: lower storage.retention_days or camera.bitrate, or free space elsewhere.\n"
                "  Diagnose: sudo ./scripts/diagnose-storage.sh",
                human_bytes(final.used_bytes),
                human_bytes(final.free_bytes),
                settings.max_usage_percent,
                settings.min_free_gb,
            )
        elif not result.still_over_limit:
            self._warned_full = False

        if result.deleted_events:
            log.info(
                "Retention removed %d event(s) and freed %s", result.deleted_events, human_bytes(result.freed_bytes)
            )
        return result

    def _free_space(self, result: CleanupResult, status: StorageStatus) -> None:
        """Delete oldest completed events until the usage limits are satisfied."""
        settings = self._config.storage
        free = status.free_bytes
        used = status.used_bytes
        total = status.total_bytes
        for event in self._store.iter_events(newest_first=False):
            if not self._over_limit(used, total, free):
                break
            if event.active:
                continue
            size = dir_size_bytes(event.directory)
            self._store.delete_directory(event.directory)
            free += size
            used -= size
            result.deleted_events += 1
            result.freed_bytes += size
            result.reasons["disk_limit"] = result.reasons.get("disk_limit", 0) + 1
            log.info(
                "Deleted event %s to stay under the storage limit (freed %s)", event.event_id, human_bytes(size)
            )

    def _over_limit(self, used: int, total: int, free: int) -> bool:
        """True when either the percentage limit or the free-space floor is breached."""
        settings = self._config.storage
        if total and (used / total * 100) > settings.max_usage_percent:
            return True
        return free < settings.min_free_gb * 1024 ** 3

    def recover_interrupted(self) -> List[str]:
        """Mark events that were mid-flight when the service stopped. Called at startup."""
        recovered: List[str] = []
        for event in self._store.iter_events():
            if not event.active:
                continue
            event.status = EventStatus.INTERRUPTED.value
            if not event.ended_at:
                event.ended_at = event.created_at or event.started_at
            for segment in event.motion_segments:
                if not segment.get("end"):
                    segment["end"] = event.ended_at
            event.finalize_reason = event.finalize_reason or "service_restart"
            self._store.save(event)
            recovered.append(event.event_id)
        if recovered:
            log.warning(
                "%d event(s) were interrupted by a restart or power loss and are marked 'interrupted': %s. "
                "Their video is recovered from the rolling buffer if it is still there.",
                len(recovered),
                ", ".join(recovered[:5]) + (" ..." if len(recovered) > 5 else ""),
            )
        return recovered
