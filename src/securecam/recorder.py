"""Turns a finished event into an MP4 by cutting the range out of the rolling buffer."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Optional

from .config import Config
from .events import Event, EventStore, TaskState
from .logging_setup import get_logger
from .mediamtx import MediaMTXClient
from .util import HttpError, parse_rfc3339, utcnow

log = get_logger("recorder")

# Retries have to be fast: the source segments are deleted once the rolling
# buffer window passes, so a slow backoff would simply lose the video.
RETRY_DELAYS = (5.0, 15.0, 30.0, 60.0, 120.0)


class ClipRecorder:
    """Extracts event clips. No transcoding happens anywhere in this path."""

    def __init__(self, config: Config, client: MediaMTXClient, store: EventStore) -> None:
        self._config = config
        self._client = client
        self._store = store

    def extract(self, event: Event) -> bool:
        """Produce recording.mp4 for an event. Returns True on success."""
        if not event.ended_at:
            event.recording.give_up("event has no end time")
            self._store.save(event)
            return False

        start = parse_rfc3339(event.clip_start_at)
        end = parse_rfc3339(event.ended_at)
        duration = (end - start).total_seconds()
        if duration <= 0:
            event.recording.give_up("computed clip duration was not positive")
            self._store.save(event)
            return False

        deadline = start + timedelta(seconds=self._config.buffer_retain_seconds - 60)
        event.recording.begin()
        event.recording.requested_start = event.clip_start_at
        event.recording.requested_duration_seconds = round(duration, 3)
        self._store.save(event)

        try:
            segments = self._client.list_segments(start - timedelta(seconds=5), end + timedelta(seconds=5))
        except HttpError as exc:
            return self._retry_or_lose(event, f"the playback server could not be queried: {exc}", deadline)

        if not segments:
            event.recording.state = TaskState.LOST.value
            event.recording.last_error = "no buffered video covers this time range"
            self._store.save(event)
            log.error(
                "Event %s has no video: the rolling buffer holds nothing between %s and %s.\n"
                "  What still works: the event, its snapshot and its notification are intact.\n"
                "  Likely causes: the camera was not publishing during the event, storage.buffer_retain_minutes\n"
                "  is too short, or the disk filled up and MediaMTX could not write segments.\n"
                "  Diagnose: sudo ./scripts/diagnose-camera.sh && sudo ./scripts/diagnose-storage.sh",
                event.event_id,
                event.clip_start_at,
                event.ended_at,
            )
            return False

        available_start = min(segment.start for segment in segments)
        if available_start > start:
            gap = (available_start - start).total_seconds()
            log.warning(
                "Event %s loses %.1fs of pre-event video: the buffer only reaches back to %s. "
                "Increase storage.buffer_retain_minutes if this keeps happening.",
                event.event_id,
                gap,
                available_start.isoformat(),
            )
            start = available_start
            duration = (end - start).total_seconds()
            event.recording.requested_start = event.clip_start_at
            event.recording.requested_duration_seconds = round(duration, 3)

        destination = event.recording_path
        try:
            size = self._client.download_clip(destination, start, duration)
        except HttpError as exc:
            return self._retry_or_lose(event, f"clip extraction failed: {exc}", deadline)
        except OSError as exc:
            return self._retry_or_lose(event, f"clip could not be written to disk: {exc}", deadline)

        event.recording.path = os.path.basename(destination)
        event.recording.size_bytes = size
        event.recording.succeed()
        self._store.save(event)
        log.info(
            "Saved %s (%.1fs, %d bytes) for event %s",
            os.path.basename(destination),
            duration,
            size,
            event.event_id,
        )
        return True

    def _retry_or_lose(self, event: Event, message: str, deadline) -> bool:
        """Schedule another attempt while the buffer still holds the source video."""
        attempt = max(0, event.recording.attempts - 1)
        delay: Optional[float] = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
        if utcnow() + timedelta(seconds=delay or 0) >= deadline:
            event.recording.state = TaskState.LOST.value
            event.recording.last_error = f"{message} (the rolling buffer expired before it could be retried)"
            event.recording.next_attempt_at = None
            self._store.save(event)
            log.error(
                "Giving up on the video for event %s: %s. The event metadata, snapshot and notification are kept.",
                event.event_id,
                message,
            )
            return False
        event.recording.fail(message, retry_in=delay)
        self._store.save(event)
        log.warning("Clip extraction for event %s failed (%s); retrying in %.0fs", event.event_id, message, delay)
        return False
