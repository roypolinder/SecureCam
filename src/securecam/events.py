"""Event records: on-disk layout, metadata schema, and queries over them."""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional

from .logging_setup import get_logger
from .util import atomic_write_json, ensure_dir, new_id, parse_rfc3339, read_json, to_rfc3339, utcnow

log = get_logger("events")

SCHEMA_VERSION = 1
METADATA_NAME = "metadata.json"
RECORDING_NAME = "recording.mp4"
SNAPSHOT_NAME = "snapshot.jpg"


class TaskState(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    LOST = "lost"

    @property
    def terminal(self) -> bool:
        """True when no further work will be attempted."""
        return self in (TaskState.COMPLETED, TaskState.SKIPPED, TaskState.LOST)


class EventStatus(str, Enum):
    RECORDING = "recording"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


@dataclass
class TaskInfo:
    state: str = TaskState.PENDING.value
    attempts: int = 0
    last_error: str = ""
    last_attempt_at: Optional[str] = None
    completed_at: Optional[str] = None
    next_attempt_at: Optional[str] = None

    def begin(self) -> None:
        """Mark an attempt as started."""
        self.state = TaskState.IN_PROGRESS.value
        self.attempts += 1
        self.last_attempt_at = to_rfc3339(utcnow())

    def succeed(self) -> None:
        self.state = TaskState.COMPLETED.value
        self.last_error = ""
        self.next_attempt_at = None
        self.completed_at = to_rfc3339(utcnow())

    def fail(self, message: str, retry_in: Optional[float] = None) -> None:
        """Record a failure and optionally schedule the next retry."""
        self.state = TaskState.PENDING.value if retry_in is not None else TaskState.FAILED.value
        self.last_error = message[:500]
        self.next_attempt_at = to_rfc3339(utcnow() + timedelta(seconds=retry_in)) if retry_in is not None else None

    def give_up(self, message: str) -> None:
        """Stop retrying permanently."""
        self.state = TaskState.FAILED.value
        self.last_error = message[:500]
        self.next_attempt_at = None

    def skip(self, reason: str = "") -> None:
        self.state = TaskState.SKIPPED.value
        self.last_error = reason[:500]
        self.next_attempt_at = None

    @property
    def due(self) -> bool:
        """True when this task is waiting and its retry delay has elapsed."""
        if self.state not in (TaskState.PENDING.value, TaskState.IN_PROGRESS.value):
            return False
        if not self.next_attempt_at:
            return True
        try:
            return utcnow() >= parse_rfc3339(self.next_attempt_at)
        except ValueError:
            return True


@dataclass
class RecordingInfo(TaskInfo):
    path: Optional[str] = None
    size_bytes: int = 0
    requested_start: Optional[str] = None
    requested_duration_seconds: float = 0.0


@dataclass
class SnapshotInfo(TaskInfo):
    paths: List[str] = field(default_factory=list)


@dataclass
class AIInfo(TaskInfo):
    provider: str = ""
    person_detected: Optional[bool] = None
    confidence: Optional[float] = None
    label: str = ""
    summary: str = ""
    checks: int = 0
    false_positive: bool = False


@dataclass
class NotificationInfo(TaskInfo):
    sent_at: Optional[str] = None
    results: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class MotionSegment:
    start: str
    end: Optional[str] = None


@dataclass
class Event:
    event_id: str
    device_id: str
    device_name: str = ""
    trigger: str = "pir"
    status: str = EventStatus.RECORDING.value
    created_at: str = ""
    started_at: str = ""
    clip_start_at: str = ""
    ended_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    finalize_reason: Optional[str] = None
    schema_version: int = SCHEMA_VERSION
    motion_segments: List[Dict[str, Any]] = field(default_factory=list)
    recording: RecordingInfo = field(default_factory=RecordingInfo)
    snapshot: SnapshotInfo = field(default_factory=SnapshotInfo)
    ai: AIInfo = field(default_factory=AIInfo)
    notification: NotificationInfo = field(default_factory=NotificationInfo)
    directory: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializable form written to metadata.json."""
        data = asdict(self)
        data.pop("directory", None)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any], directory: str = "") -> "Event":
        """Rebuild an event from metadata.json, tolerating missing optional sections."""
        payload = dict(data)
        payload.pop("directory", None)
        event = cls(
            event_id=str(payload.get("event_id", "")),
            device_id=str(payload.get("device_id", "")),
            device_name=str(payload.get("device_name", "")),
            trigger=str(payload.get("trigger", "pir")),
            status=str(payload.get("status", EventStatus.COMPLETED.value)),
            created_at=str(payload.get("created_at", "")),
            started_at=str(payload.get("started_at", "")),
            clip_start_at=str(payload.get("clip_start_at", "")),
            ended_at=payload.get("ended_at"),
            duration_seconds=payload.get("duration_seconds"),
            finalize_reason=payload.get("finalize_reason"),
            schema_version=int(payload.get("schema_version", SCHEMA_VERSION)),
            motion_segments=list(payload.get("motion_segments", []) or []),
            recording=_task(RecordingInfo, payload.get("recording")),
            snapshot=_task(SnapshotInfo, payload.get("snapshot")),
            ai=_task(AIInfo, payload.get("ai")),
            notification=_task(NotificationInfo, payload.get("notification")),
            directory=directory,
        )
        return event

    @property
    def started(self) -> datetime:
        return parse_rfc3339(self.started_at)

    @property
    def age_seconds(self) -> float:
        """Seconds since the event was triggered."""
        try:
            return (utcnow() - parse_rfc3339(self.created_at or self.started_at)).total_seconds()
        except ValueError:
            return 0.0

    @property
    def recording_path(self) -> str:
        return os.path.join(self.directory, RECORDING_NAME)

    @property
    def snapshot_path(self) -> str:
        return os.path.join(self.directory, SNAPSHOT_NAME)

    @property
    def metadata_path(self) -> str:
        return os.path.join(self.directory, METADATA_NAME)

    @property
    def active(self) -> bool:
        """True while the event is still being produced and must not be deleted."""
        return self.status in (EventStatus.RECORDING.value, EventStatus.FINALIZING.value)

    def summary(self) -> Dict[str, Any]:
        """Compact form used by list endpoints and the web UI."""
        return {
            "event_id": self.event_id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "finalize_reason": self.finalize_reason,
            "has_recording": self.recording.state == TaskState.COMPLETED.value,
            "recording_state": self.recording.state,
            "has_snapshot": bool(self.snapshot.paths),
            "ai_state": self.ai.state,
            "person_detected": self.ai.person_detected,
            "confidence": self.ai.confidence,
            "ai_checks": self.ai.checks,
            "false_positive": self.ai.false_positive,
            "notification_state": self.notification.state,
            "size_bytes": self.recording.size_bytes,
        }


def _task(cls, payload: Any):
    """Rebuild a task section, ignoring unknown keys from newer versions."""
    instance = cls()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if hasattr(instance, key):
                setattr(instance, key, value)
    return instance


class EventStore:
    """Creates, saves and queries event directories under a single root."""

    def __init__(self, root: str) -> None:
        self.root = root
        self._lock = threading.RLock()
        self._index: Dict[str, str] = {}
        ensure_dir(self.root)

    # -- creation -----------------------------------------------------------

    def create(
        self,
        device_id: str,
        device_name: str,
        started_at: datetime,
        pre_event_seconds: float,
        trigger: str = "pir",
    ) -> Event:
        """Create the event directory and write the initial metadata immediately."""
        stamp = started_at.strftime("%Y%m%d_%H%M%S")
        event_id = f"event_{stamp}_{new_id()}"
        directory = os.path.join(
            self.root, started_at.strftime("%Y"), started_at.strftime("%m"), started_at.strftime("%d"), event_id
        )
        ensure_dir(directory)
        event = Event(
            event_id=event_id,
            device_id=device_id,
            device_name=device_name,
            trigger=trigger,
            status=EventStatus.RECORDING.value,
            created_at=to_rfc3339(utcnow()),
            started_at=to_rfc3339(started_at),
            clip_start_at=to_rfc3339(started_at - timedelta(seconds=pre_event_seconds)),
            directory=directory,
        )
        event.motion_segments.append({"start": event.started_at, "end": None})
        self.save(event)
        with self._lock:
            self._index[event_id] = directory
        return event

    def save(self, event: Event) -> None:
        """Persist metadata atomically so a crash never leaves a half-written file."""
        if not event.directory:
            raise ValueError("event has no directory")
        atomic_write_json(event.metadata_path, event.to_dict())

    # -- lookup -------------------------------------------------------------

    def load(self, directory: str) -> Optional[Event]:
        """Load one event directory, returning None when it is unreadable."""
        path = os.path.join(directory, METADATA_NAME)
        try:
            data = read_json(path)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("Skipping unreadable event metadata %s: %s", path, exc)
            return None
        if not isinstance(data, dict):
            log.warning("Skipping malformed event metadata %s", path)
            return None
        return Event.from_dict(data, directory)

    def get(self, event_id: str) -> Optional[Event]:
        """Find an event by id, using a cached directory index."""
        if not _valid_event_id(event_id):
            return None
        with self._lock:
            directory = self._index.get(event_id)
        if directory and os.path.isdir(directory):
            return self.load(directory)
        for event in self.iter_events():
            if event.event_id == event_id:
                return event
        return None

    def iter_events(self, newest_first: bool = True) -> Iterator[Event]:
        """Walk the date-partitioned tree in chronological order."""
        for directory in self.iter_directories(newest_first=newest_first):
            event = self.load(directory)
            if event is not None:
                with self._lock:
                    self._index[event.event_id] = directory
                yield event

    def iter_directories(self, newest_first: bool = True) -> Iterator[str]:
        """Yield event directory paths without parsing metadata."""
        for year in _sorted_children(self.root, newest_first):
            for month in _sorted_children(os.path.join(self.root, year), newest_first):
                day_root = os.path.join(self.root, year, month)
                for day in _sorted_children(day_root, newest_first):
                    event_root = os.path.join(day_root, day)
                    for name in _sorted_children(event_root, newest_first):
                        yield os.path.join(event_root, name)

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str = "",
        person_only: bool = False,
    ) -> List[Event]:
        """Newest-first page of events with optional filters."""
        results: List[Event] = []
        skipped = 0
        for event in self.iter_events():
            if status and event.status != status:
                continue
            if person_only and not event.ai.person_detected:
                continue
            if skipped < offset:
                skipped += 1
                continue
            results.append(event)
            if len(results) >= limit:
                break
        return results

    def count(self) -> int:
        """Total number of stored events."""
        return sum(1 for _ in self.iter_directories())

    def pending(self, kinds: tuple = ("ai", "notification", "recording")) -> List[Event]:
        """Events with unfinished background work that is due for another attempt."""
        due: List[Event] = []
        for event in self.iter_events():
            if event.active:
                continue
            for kind in kinds:
                task: TaskInfo = getattr(event, kind)
                if task.due:
                    due.append(event)
                    break
        return due

    def delete(self, event_id: str) -> bool:
        """Remove an event directory. Refuses while the event is still being produced."""
        event = self.get(event_id)
        if event is None:
            return False
        if event.active:
            raise ValueError(f"event {event_id} is still {event.status} and cannot be deleted yet")
        shutil.rmtree(event.directory, ignore_errors=True)
        with self._lock:
            self._index.pop(event_id, None)
        _prune_empty_parents(event.directory, self.root)
        return True

    def delete_directory(self, directory: str) -> None:
        """Remove an event directory that was already validated by the caller."""
        shutil.rmtree(directory, ignore_errors=True)
        _prune_empty_parents(directory, self.root)


def _sorted_children(path: str, newest_first: bool) -> List[str]:
    """List subdirectory names sorted by name, which is chronological for our layout."""
    try:
        names = [entry.name for entry in os.scandir(path) if entry.is_dir()]
    except OSError:
        return []
    names.sort(reverse=newest_first)
    return names


def _prune_empty_parents(directory: str, root: str) -> None:
    """Remove now-empty day/month/year folders left behind by a deletion."""
    parent = os.path.dirname(os.path.abspath(directory))
    root = os.path.abspath(root)
    while parent.startswith(root) and parent != root:
        try:
            os.rmdir(parent)
        except OSError:
            return
        parent = os.path.dirname(parent)


def _valid_event_id(event_id: str) -> bool:
    """Guard against path traversal via crafted event ids."""
    return bool(event_id) and all(char.isalnum() or char in "_-" for char in event_id)
