"""Per-event work: snapshot, AI analysis, notification, clip extraction.

None of this ever blocks motion detection or recording. Every step is retried
independently and its state is persisted in the event's metadata.json, so a
reboot or an outage cannot silently drop work.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .ai import AIError, AIProvider, AIResult, create_provider as create_ai_provider
from .config import Config
from .events import Event, EventStore, EventStatus, TaskState
from .logging_setup import get_logger, reset_event_id, set_event_id
from .mediamtx import MediaMTXClient
from .networking import NetworkMonitor
from .notifications import Notification, NotificationError, create_provider as create_notification_provider
from .notifications.base import DeliveryResult, NotificationProvider
from .recorder import ClipRecorder
from .snapshot import SnapshotCapturer, SnapshotError
from .util import Backoff, parse_rfc3339, to_rfc3339, utcnow

log = get_logger("pipeline")


class EventPipeline:
    """Runs the background work attached to an event."""

    def __init__(
        self,
        config: Config,
        store: EventStore,
        client: MediaMTXClient,
        snapshotter: SnapshotCapturer,
        network: NetworkMonitor,
        is_armed: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._config = config
        self._store = store
        self._snapshotter = snapshotter
        self._network = network
        self._is_armed = is_armed or (lambda: True)
        self._recorder = ClipRecorder(config, client, store)
        self._ai: AIProvider = create_ai_provider(config.ai)
        self._providers: Dict[str, NotificationProvider] = {}
        self._last_notification_at = 0.0
        self._lock = threading.Lock()

    # -- entry points -------------------------------------------------------

    def on_event_started(self, event: Event) -> None:
        """Snapshot, analyze and notify. Called on a worker thread right after a trigger."""
        token = set_event_id(event.event_id)
        try:
            self._capture_snapshots(event)
            self._run_ai(event)
            self._notify(event)
        except Exception:
            log.exception("Unhandled error while processing the start of event %s", event.event_id)
        finally:
            reset_event_id(token)

    def on_event_finalized(self, event: Event) -> None:
        """Extract the clip once the event has ended."""
        token = set_event_id(event.event_id)
        try:
            # MediaMTX flushes recording parts once per second; give the tail time to land.
            time.sleep(2.0)
            self._recorder.extract(event)
            event.status = EventStatus.COMPLETED.value
            self._store.save(event)
        except Exception:
            log.exception("Unhandled error while finalizing event %s", event.event_id)
            event.status = EventStatus.COMPLETED.value
            event.recording.fail("finalization crashed", retry_in=30)
            self._store.save(event)
        finally:
            reset_event_id(token)

    def process_pending(self, event: Event) -> None:
        """Retry whatever is still outstanding for an already-finished event."""
        token = set_event_id(event.event_id)
        try:
            if event.recording.due and event.recording.state != TaskState.LOST.value:
                self._recorder.extract(event)
            if event.snapshot.due and not event.snapshot.paths:
                self._capture_snapshots(event)
            if event.ai.due:
                self._run_ai(event)
            if event.notification.due:
                self._notify(event)
        except Exception:
            log.exception("Unhandled error while retrying work for event %s", event.event_id)
        finally:
            reset_event_id(token)

    # -- steps --------------------------------------------------------------

    def _capture_snapshots(self, event: Event) -> None:
        """Grab one or more JPEG frames for the UI, the AI and the notification."""
        if event.snapshot.state == TaskState.COMPLETED.value:
            return
        if not self._snapshotter.available:
            event.snapshot.skip("ffmpeg is not installed")
            self._store.save(event)
            return

        wanted = self._config.ai.snapshot_count if self._config.ai.enabled else 1
        event.snapshot.begin()
        self._store.save(event)

        paths: List[str] = []
        last_error = ""
        for index in range(max(1, wanted)):
            name = "snapshot.jpg" if index == 0 else f"snapshot_{index + 1}.jpg"
            destination = os.path.join(event.directory, name)
            try:
                self._snapshotter.capture(destination)
                paths.append(name)
            except SnapshotError as exc:
                last_error = str(exc)
                break
            if index + 1 < wanted and self._config.ai.snapshot_interval_seconds > 0:
                time.sleep(self._config.ai.snapshot_interval_seconds)

        event.snapshot.paths = paths
        if paths:
            event.snapshot.primary = event.snapshot.primary or paths[0]
            event.snapshot.succeed()
        elif self._within_retry_window(event, self._config.ai.max_retry_age_hours):
            event.snapshot.fail(last_error or "no frame was produced", retry_in=self._delay(event.snapshot.attempts))
            log.warning("Snapshot for event %s failed (%s); it will be retried", event.event_id, last_error)
        else:
            event.snapshot.give_up(last_error or "no frame was produced")
            log.error(
                "No snapshot could be taken for event %s (%s).\n"
                "  What still works: the video clip and the notification (without an image).\n"
                "  Likely causes: the camera stream is down, or FFmpeg cannot reach the local RTSP server.\n"
                "  Diagnose: sudo ./scripts/diagnose-camera.sh",
                event.event_id,
                last_error,
            )
        self._store.save(event)

    def _run_ai(self, event: Event) -> None:
        """Ask the configured provider whether a person is in frame."""
        if not self._config.ai.enabled or not self._ai.enabled:
            if event.ai.state != TaskState.SKIPPED.value:
                event.ai.skip("AI analysis is disabled")
                event.ai.provider = "disabled"
                self._store.save(event)
            return
        if event.ai.state == TaskState.COMPLETED.value:
            return
        if not self._is_armed():
            # The provider costs money per call, so a disarmed camera never makes one.
            event.ai.skip("the camera was disarmed")
            self._store.save(event)
            log.info("Skipped AI analysis for event %s: the camera is disarmed", event.event_id)
            return

        names, images = self._read_frames(event, event.snapshot.paths)
        if not images:
            event.ai.skip("no snapshot was available")
            self._store.save(event)
            return
        self._ask_ai(event, names, images)

    def recheck(self, event: Event) -> bool:
        """Look again during a long event. A car can trigger the sensor before its driver steps out."""
        token = set_event_id(event.event_id)
        try:
            if not self._recheck_due(event):
                return False
            names, images = self._capture_recheck_frames(event)
            if not images:
                return False
            before = event.ai.person_detected
            self._ask_ai(event, names, images)
            if event.ai.person_detected and not before:
                log.info(
                    "Re-check %d found a person in event %s that the first look missed",
                    event.ai.checks,
                    event.event_id,
                )
                if event.notification.state == TaskState.SKIPPED.value:
                    event.notification.state = TaskState.PENDING.value
                    event.notification.last_error = ""
                    event.notification.next_attempt_at = None
                    self._store.save(event)
                self._notify(event)
            return True
        except Exception:
            log.exception("Unhandled error while re-checking event %s", event.event_id)
            return False
        finally:
            reset_event_id(token)

    def _recheck_due(self, event: Event) -> bool:
        """Only look again when the first verdict landed, found nobody, and the budget allows it."""
        if not self._config.ai.enabled or not self._ai.enabled or not self._is_armed():
            return False
        if self._config.ai.recheck_interval_seconds <= 0:
            return False
        # A failed or queued verdict is the pending worker's job; stacking calls would double the bill.
        if event.ai.state != TaskState.COMPLETED.value:
            return False
        if event.ai.person_detected:
            return False
        return event.ai.checks < self._config.ai.max_checks

    def _capture_recheck_frames(self, event: Event) -> Tuple[List[str], List[bytes]]:
        """Grab a fresh batch and keep it with the event, so the UI shows what each check saw."""
        wanted = max(1, self._config.ai.snapshot_count)
        interval = self._config.ai.snapshot_interval_seconds
        names: List[str] = []
        for index in range(wanted):
            name = f"recheck{event.ai.checks}_{index + 1}.jpg"
            destination = os.path.join(event.directory, name)
            try:
                self._snapshotter.capture(destination)
            except SnapshotError as exc:
                log.warning("Could not take a re-check frame for event %s: %s", event.event_id, exc)
                break
            names.append(name)
            if index + 1 < wanted and interval > 0:
                time.sleep(interval)
        if not names:
            return [], []
        event.snapshot.paths.extend(names)
        self._store.save(event)
        return self._read_frames(event, names)

    def _ask_ai(self, event: Event, names: List[str], images: List[bytes]) -> None:
        """One provider call plus the bookkeeping around its verdict."""
        if event.ai.checks >= self._config.ai.max_checks:
            event.ai.skip(f"reached the limit of {self._config.ai.max_checks} AI checks for this event")
            self._store.save(event)
            return
        if not self._network.online:
            event.ai.fail(
                "no internet connectivity", retry_in=self._delay(event.ai.attempts, self._config.network.retry_initial_seconds)
            )
            self._store.save(event)
            log.info("AI analysis for event %s is queued until connectivity returns", event.event_id)
            return

        event.ai.begin()
        event.ai.provider = self._ai.name
        self._store.save(event)

        try:
            result: AIResult = self._ai.analyze(images, {"device_name": self._config.device.name, "event_id": event.event_id})
        except AIError as exc:
            self._handle_ai_failure(event, exc)
            return
        except Exception as exc:
            self._handle_ai_failure(event, AIError(f"unexpected provider error: {exc}", retryable=False))
            return

        confidence = result.confidence
        detected = result.person_detected
        if detected and confidence is not None and confidence < self._config.ai.min_confidence:
            detected = False
        event.ai.person_detected = detected
        event.ai.confidence = confidence
        event.ai.label = result.label
        event.ai.summary = result.summary
        event.ai.checks += 1
        event.ai.false_positive = not detected and event.ai.checks >= self._config.ai.max_checks
        if names:
            # Show the frame the verdict came from, not the one from the start of the event.
            event.snapshot.primary = names[-1]
        event.ai.succeed()
        self._store.save(event)
        log.info(
            "AI result for event %s (check %d/%d): person=%s confidence=%s (%s)",
            event.event_id,
            event.ai.checks,
            self._config.ai.max_checks,
            detected,
            f"{confidence:.2f}" if confidence is not None else "n/a",
            self._ai.name,
        )
        if event.ai.false_positive:
            log.info("Event %s is marked a false positive: %d checks found nobody", event.event_id, event.ai.checks)

    def _handle_ai_failure(self, event: Event, exc: AIError) -> None:
        """Record an AI failure and decide whether it is worth retrying."""
        message = str(exc)
        if exc.hint:
            message = f"{message} - {exc.hint}"
        if exc.retryable and self._within_retry_window(event, self._config.ai.max_retry_age_hours):
            event.ai.fail(message, retry_in=self._delay(event.ai.attempts))
            log.warning("AI analysis for event %s failed (%s); it will be retried", event.event_id, message)
        else:
            event.ai.give_up(message)
            log.error(
                "AI analysis for event %s will not be retried: %s\n"
                "  What still works: the event, its video, its snapshot and its notification.\n"
                "  Only the automatic person/no-person label is missing.",
                event.event_id,
                message,
            )
        self._store.save(event)

    def _notify(self, event: Event) -> None:
        """Send the alarm to every enabled recipient."""
        settings = self._config.notifications
        if not settings.enabled:
            if event.notification.state != TaskState.SKIPPED.value:
                event.notification.skip("notifications are disabled")
                self._store.save(event)
            return
        if event.notification.state == TaskState.COMPLETED.value:
            return
        if not self._is_armed():
            event.notification.skip("the camera was disarmed")
            self._store.save(event)
            log.info("Suppressed the notification for event %s: the camera is disarmed", event.event_id)
            return
        if event.ai.due and self._config.ai.enabled:
            return  # wait for the AI verdict; the pending worker will come back

        if settings.only_if_person and event.ai.person_detected is False:
            event.notification.skip("AI reported no person")
            self._store.save(event)
            log.info("Suppressed the notification for event %s: no person detected", event.event_id)
            return

        recipients = settings.active_recipients()
        if not recipients:
            event.notification.skip("no enabled recipients")
            self._store.save(event)
            return

        if settings.cooldown_seconds > 0:
            with self._lock:
                since = time.monotonic() - self._last_notification_at
                if self._last_notification_at and since < settings.cooldown_seconds:
                    event.notification.skip(f"suppressed by the {settings.cooldown_seconds:.0f}s cooldown")
                    self._store.save(event)
                    return

        if not self._network.online:
            event.notification.fail(
                "no internet connectivity",
                retry_in=self._delay(event.notification.attempts, self._config.network.retry_initial_seconds),
            )
            self._store.save(event)
            log.info("Notification for event %s is queued until connectivity returns", event.event_id)
            return

        event.notification.begin()
        self._store.save(event)

        notification = self._build_notification(event)
        results: List[DeliveryResult] = []
        for recipient in recipients:
            provider = self._provider_for(recipient.provider or settings.provider)
            try:
                provider.send(notification, recipient)
                results.append(DeliveryResult(recipient.id, provider.name, True))
            except NotificationError as exc:
                detail = f"{exc}{' - ' + exc.hint if exc.hint else ''}"
                results.append(DeliveryResult(recipient.id, provider.name, False, detail))
            except Exception as exc:
                results.append(DeliveryResult(recipient.id, provider.name, False, f"unexpected error: {exc}"))

        event.notification.results = [
            {"recipient": r.recipient_id, "provider": r.provider, "ok": r.ok, "error": r.error} for r in results
        ]
        succeeded = [r for r in results if r.ok]
        failed = [r for r in results if not r.ok]

        if succeeded and not failed:
            event.notification.sent_at = to_rfc3339(utcnow())
            event.notification.succeed()
            with self._lock:
                self._last_notification_at = time.monotonic()
            log.info("Notified %d recipient(s) about event %s", len(succeeded), event.event_id)
        elif self._within_retry_window(event, settings.max_retry_age_hours):
            summary = "; ".join(f"{r.recipient_id}: {r.error}" for r in failed)
            event.notification.fail(summary, retry_in=self._delay(event.notification.attempts))
            if succeeded:
                event.notification.sent_at = to_rfc3339(utcnow())
            log.warning("Notification for event %s partially failed (%s); retrying", event.event_id, summary)
        else:
            summary = "; ".join(f"{r.recipient_id}: {r.error}" for r in failed)
            event.notification.give_up(summary)
            log.error(
                "Notifications for event %s could not be delivered: %s\n"
                "  What still works: the event, its video and its snapshot are saved and visible in the UI.\n"
                "  Test delivery with: sudo securecam-admin test-notify",
                event.event_id,
                summary,
            )
        self._store.save(event)

    # -- helpers ------------------------------------------------------------

    def _build_notification(self, event: Event) -> Notification:
        """Compose the alarm text, image and deep link."""
        settings = self._config.notifications
        device = self._config.device.name or self._config.device.id
        started = event.started_at
        lines = [f"Motion detected at {device}", started.replace("T", " ").split(".")[0] + " UTC"]

        if event.ai.state == TaskState.COMPLETED.value:
            if event.ai.person_detected:
                confidence = f" ({event.ai.confidence:.0%} confident)" if event.ai.confidence is not None else ""
                lines.append(f"PERSON DETECTED{confidence}")
            elif event.ai.person_detected is False:
                lines.append("No person detected")
            if event.ai.summary:
                lines.append(event.ai.summary)
        elif self._config.ai.enabled:
            lines.append("AI analysis unavailable")

        snapshot: Optional[bytes] = None
        if settings.include_snapshot:
            _, images = self._read_frames(event, [event.snapshot.primary] if event.snapshot.primary else event.snapshot.paths[:1])
            snapshot = images[0] if images else None

        link = ""
        if self._config.api.public_base_url:
            link = f"{self._config.api.public_base_url}/#event={event.event_id}"

        title = f"{'PERSON' if event.ai.person_detected else 'Motion'} - {device}"
        return Notification(
            title=title,
            message="\n".join(lines),
            event_id=event.event_id,
            device_name=device,
            alarm=settings.alarm,
            link=link,
            snapshot=snapshot,
            tags=list(settings.ntfy.tags),
        )

    def _provider_for(self, name: str) -> NotificationProvider:
        """Cache one provider instance per configured backend."""
        key = name or self._config.notifications.provider
        with self._lock:
            provider = self._providers.get(key)
            if provider is None:
                provider = create_notification_provider(self._config.notifications, key)
                self._providers[key] = provider
            return provider

    def _read_frames(self, event: Event, names: Sequence[str]) -> Tuple[List[str], List[bytes]]:
        """Read the named snapshots back, skipping any that no longer exist."""
        kept: List[str] = []
        images: List[bytes] = []
        for name in names:
            base = os.path.basename(name)
            try:
                with open(os.path.join(event.directory, base), "rb") as handle:
                    images.append(handle.read())
            except OSError:
                continue
            kept.append(base)
        return kept, images

    def _within_retry_window(self, event: Event, max_age_hours: float) -> bool:
        """True while an event is young enough to keep retrying."""
        if max_age_hours <= 0:
            return False
        try:
            created = parse_rfc3339(event.created_at or event.started_at)
        except ValueError:
            return False
        return utcnow() - created < timedelta(hours=max_age_hours)

    def _delay(self, attempts: int, initial: Optional[float] = None) -> float:
        """Exponential backoff derived from the number of attempts already made."""
        backoff = Backoff(initial or self._config.network.retry_initial_seconds, self._config.network.retry_max_seconds)
        delay = 0.0
        for _ in range(max(1, attempts)):
            delay = backoff.next_delay()
        return delay

    def check_providers(self) -> List[str]:
        """Configuration problems that would make AI or notifications fail at runtime."""
        problems: List[str] = []
        if self._config.ai.enabled:
            ok, message = self._ai.check()
            if not ok:
                problems.append(f"ai: {message}")
        if self._config.notifications.enabled:
            names = {r.provider or self._config.notifications.provider for r in self._config.notifications.active_recipients()}
            for name in names:
                ok, message = self._provider_for(name).check()
                if not ok:
                    problems.append(f"notifications ({name}): {message}")
        return problems

    def send_test_notification(self) -> List[DeliveryResult]:
        """Send a test alarm to every enabled recipient, used by securecam-admin."""
        settings = self._config.notifications
        device = self._config.device.name or self._config.device.id
        notification = Notification(
            title=f"SecureCam test - {device}",
            message="This is a test alarm from SecureCam. If your phone did not wake you up, "
            "check the notification channel settings in the app.",
            device_name=device,
            alarm=settings.alarm,
            link=self._config.api.public_base_url,
            tags=list(settings.ntfy.tags),
        )
        results: List[DeliveryResult] = []
        for recipient in settings.active_recipients():
            provider = self._provider_for(recipient.provider or settings.provider)
            try:
                provider.send(notification, recipient)
                results.append(DeliveryResult(recipient.id, provider.name, True))
            except NotificationError as exc:
                results.append(DeliveryResult(recipient.id, provider.name, False, f"{exc} {exc.hint}".strip()))
            except Exception as exc:
                results.append(DeliveryResult(recipient.id, provider.name, False, f"unexpected error: {exc}"))
        return results

    @property
    def ai_provider(self) -> AIProvider:
        return self._ai
