"""Service entry point: wires every subsystem together and runs the control loop."""

from __future__ import annotations

import argparse
import os
import secrets
import signal
import socket
import sys
import threading
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .api import ApiServer
from .appcontext import AppContext
from .arming import ArmingState
from .auth import TokenSigner, UserStore
from .config import DEFAULT_CONFIG_PATH, SECRET_KEY_PATH, USERS_PATH, Config, ConfigError, load_config
from .device import load_env_file, load_secret_key, resolve_device_id
from .diagnostics import build_report, format_report
from .events import Event, EventStore, EventStatus
from .health import DEGRADED, OK, UNKNOWN, Check, HealthMonitor
from .logging_setup import configure_logging, get_logger, set_device_id
from .mediamtx import MediaMTXClient, MediaMTXSupervisor, write_config
from .networking import NetworkMonitor
from .pipeline import EventPipeline
from .pir import MotionEdge, PirMonitor
from .snapshot import SnapshotCapturer
from .statemachine import ActionKind, FinalizeReason, MotionStateMachine
from .storage import StorageManager
from .util import monotonic, to_rfc3339, utcnow
from .worker import PendingWorker, TaskRunner

log = get_logger("main")

TICK_SECONDS = 0.25


class Controller:
    """Owns the event state machine and the periodic housekeeping loop."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.device_id = resolve_device_id(config)
        config.device.id = self.device_id
        set_device_id(self.device_id)

        self.signer = TokenSigner(load_secret_key(SECRET_KEY_PATH))
        self.users = UserStore(USERS_PATH, self.device_id)
        self.service_credentials = _service_credentials()

        self.store = EventStore(config.storage.events_path)
        self.storage = StorageManager(config, self.store)
        self.client = MediaMTXClient(config, *self.service_credentials)
        self.supervisor = MediaMTXSupervisor(config, self.client)
        self.network = NetworkMonitor(config.network)
        self.snapshotter = SnapshotCapturer(config, self._stream_credentials)
        self.arming = ArmingState(
            os.path.join(config.storage.base_path, "arm-state.json"), config.motion.armed_default
        )
        self.pipeline = EventPipeline(
            config, self.store, self.client, self.snapshotter, self.network, is_armed=lambda: self.arming.armed
        )
        self.tasks = TaskRunner(max_workers=2)
        self.pending = PendingWorker(config, self.store, self.pipeline, interval_seconds=60.0)
        self.network.set_on_change(self.pending.on_connectivity_change)

        self.machine = MotionStateMachine(
            config.motion.post_motion_seconds, config.motion.max_event_seconds, config.motion.cooldown_seconds
        )
        self.pir = PirMonitor(config.motion, self._on_motion_edge)
        self.health = HealthMonitor(config, self._health_checks())
        self.api: Optional[ApiServer] = None

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._current: Optional[Event] = None
        self._event_start_wall = utcnow()
        self._event_start_mono = 0.0
        self._last_storage_check = 0.0
        self._last_stream_check = 0.0
        self._started_at = utcnow()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Bring every subsystem up in dependency order."""
        log.info("SecureCam %s starting on device %s (%s)", __version__, self.device_id, self.config.device.name)

        self.storage.prepare()
        interrupted = self.storage.recover_interrupted()

        if write_config(self.config, *self.service_credentials):
            log.warning("The MediaMTX configuration changed; restarting the stream service to apply it")
            self.supervisor.restart_unit()

        self.network.check()
        self.network.start()

        if self.config.api.enabled:
            self.api = ApiServer(self.context())
            self.api.start()
            if self.users.admin_count() == 0:
                log.warning(
                    "There is no administrator account yet, so nobody can log in or watch the live stream.\n"
                    "  Create one with: sudo securecam-admin user add --username <name> --role admin"
                )
        else:
            log.warning("api.enabled is false: no web UI, no remote viewing, recording only")

        self.pir.start()
        self.pending.start()
        self.supervisor.check(allow_restart=False)
        self.health.start()
        self.health.collect()

        for problem in self.pipeline.check_providers():
            log.warning("Configuration problem: %s", problem)

        if interrupted:
            self.pending.wake()

        _notify_systemd("READY=1")
        log.info("Startup complete")

    def run(self) -> None:
        """Control loop. Everything slow happens on other threads."""
        watchdog = _watchdog_interval()
        next_ping = monotonic() + watchdog if watchdog else 0.0
        while not self._stop.is_set():
            now = monotonic()
            with self._lock:
                actions = self.machine.tick(now)
                self._apply(actions, now)

            if now - self._last_stream_check >= 15.0:
                self._last_stream_check = now
                try:
                    self.supervisor.check()
                except Exception:
                    log.exception("Stream health probe raised; the control loop continues")

            if now - self._last_storage_check >= self.config.storage.check_interval_seconds:
                self._last_storage_check = now
                self.tasks.submit(self.storage.enforce)

            if watchdog and now >= next_ping:
                next_ping = now + watchdog
                _notify_systemd("WATCHDOG=1")

            self._stop.wait(TICK_SECONDS)

    def stop(self) -> None:
        """Finalize any running event, then shut down cleanly."""
        if self._stop.is_set():
            return
        log.info("Shutting down")
        _notify_systemd("STOPPING=1")
        self._stop.set()

        with self._lock:
            actions = self.machine.force_finalize(monotonic(), FinalizeReason.SHUTDOWN)
            self._apply(actions, monotonic())

        self.pir.stop()
        self.health.stop()
        self.pending.stop()
        self.network.stop()
        if self.api is not None:
            self.api.stop()
        self.tasks.shutdown(wait=True)
        log.info("Stopped")

    def context(self) -> AppContext:
        """Handle passed to the API layer and the CLI."""
        return AppContext(
            config=self.config,
            device_id=self.device_id,
            store=self.store,
            users=self.users,
            signer=self.signer,
            storage=self.storage,
            network=self.network,
            client=self.client,
            supervisor=self.supervisor,
            pipeline=self.pipeline,
            service_credentials=self.service_credentials,
            pir=self.pir,
            health=self.health,
            arming=self.arming,
            set_armed=self.set_armed,
            controller_state=self.state,
            version=__version__,
        )

    def state(self) -> Dict[str, Any]:
        """Live controller state for diagnostics and health."""
        with self._lock:
            return {
                "state": self.machine.state.value,
                "armed": self.arming.armed,
                "motion_active": self.machine.motion_active,
                "current_event": self._current.event_id if self._current else None,
                "elapsed_seconds": round(self.machine.elapsed(monotonic()), 1),
                "quiet_remaining_seconds": round(self.machine.quiet_remaining(monotonic()), 1),
                "pending_work": self.pending.pending_count,
                "started_at": to_rfc3339(self._started_at),
            }

    # -- event handling -----------------------------------------------------

    def set_armed(self, armed: bool, actor: str = "unknown") -> Dict[str, Any]:
        """Arm or disarm motion recording. Live viewing is never affected."""
        with self._lock:
            if self.arming.set(armed, actor) and not armed:
                now = monotonic()
                self._apply(self.machine.force_finalize(now, FinalizeReason.DISARMED), now)
        self.health.collect()
        return self.arming.status()

    def _on_motion_edge(self, edge: MotionEdge, now: float) -> None:
        """Called from the PIR thread; keeps the state machine single-threaded via the lock."""
        with self._lock:
            if not self.arming.armed:
                if edge is MotionEdge.START:
                    log.info("Motion detected while disarmed; no event recorded")
                return
            if edge is MotionEdge.START:
                log.info("Motion detected")
                actions = self.machine.on_motion_start(now)
            else:
                log.info("Motion stopped; quiet period started")
                actions = self.machine.on_motion_end(now)
            self._apply(actions, now)

    def _apply(self, actions, now: float) -> None:
        """Execute the state machine's decisions. Must be called with the lock held."""
        for action in actions:
            if action.kind is ActionKind.START_EVENT:
                self._start_event(action.at)
            elif action.kind is ActionKind.MOTION_RESUMED:
                self._append_segment(action.at)
            elif action.kind is ActionKind.MOTION_PAUSED:
                self._close_segment(action.at)
            elif action.kind is ActionKind.FINALIZE_EVENT:
                self._finalize_event(action.at, action.reason)

    def _start_event(self, at: float) -> None:
        """Create the event directory immediately so nothing is lost on a crash."""
        self._event_start_wall = utcnow()
        self._event_start_mono = at
        try:
            event = self.store.create(
                self.device_id,
                self.config.device.name,
                self._event_start_wall,
                self.config.motion.pre_event_seconds,
            )
        except OSError as exc:
            log.error(
                "Could not create an event directory: %s\n"
                "  What still works: streaming and the API. This motion event is not being recorded.\n"
                "  Likely cause: the disk is full or read-only.\n"
                "  Diagnose: sudo ./scripts/diagnose-storage.sh",
                exc,
            )
            self.machine.notify_finalized(at)
            return
        self._current = event
        self.machine.mark_recording()
        log.info("Started event %s", event.event_id)
        self.tasks.submit(self.pipeline.on_event_started, event)

    def _append_segment(self, at: float) -> None:
        """Motion resumed during the quiet period."""
        if self._current is None:
            return
        self._current.motion_segments.append({"start": to_rfc3339(self._wall(at)), "end": None})
        self.store.save(self._current)
        log.info("Motion resumed; event %s continues", self._current.event_id)

    def _close_segment(self, at: float) -> None:
        """Motion stopped; close the open motion segment."""
        if self._current is None:
            return
        for segment in reversed(self._current.motion_segments):
            if segment.get("end") is None:
                segment["end"] = to_rfc3339(self._wall(at))
                break
        self.store.save(self._current)

    def _finalize_event(self, at: float, reason: Optional[FinalizeReason]) -> None:
        """Close the event and hand clip extraction to a worker thread."""
        event = self._current
        self.machine.notify_finalized(at)
        if event is None:
            return
        end = self._wall(at)
        self._close_segment(at)
        self._current = None
        event.ended_at = to_rfc3339(end)
        event.duration_seconds = round((end - self._event_start_wall).total_seconds(), 3)
        event.finalize_reason = reason.value if reason else None
        event.status = EventStatus.FINALIZING.value
        self.store.save(event)
        log.info(
            "Event %s finished after %.0fs (%s)", event.event_id, event.duration_seconds, event.finalize_reason
        )
        self.tasks.submit(self.pipeline.on_event_finalized, event)

    def _wall(self, at: float):
        """Convert a monotonic timestamp to wall clock using the event's origin."""
        return self._event_start_wall + timedelta(seconds=max(0.0, at - self._event_start_mono))

    # -- helpers ------------------------------------------------------------

    def _stream_credentials(self) -> Tuple[str, str]:
        """Short-lived read-only credentials for FFmpeg snapshots."""
        token = self.signer.issue(
            {"kind": "stream", "sub": "internal", "did": self.device_id, "path": self.config.mediamtx.path_name},
            60,
        )
        return ("ticket", token)

    def _health_checks(self) -> Dict[str, Any]:
        """Subsystem checks handed to the health monitor."""

        def camera() -> Check:
            health = self.supervisor.health()
            if health.path_ready:
                return Check("camera", OK, f"publishing {', '.join(health.tracks) or 'video'}", details=vars(health))
            if not health.probed:
                return Check("camera", UNKNOWN, "the stream has not been probed yet", details=vars(health))
            if health.unhealthy_for_seconds < 45:
                return Check(
                    "camera",
                    DEGRADED,
                    f"waiting for the camera to publish ({health.unhealthy_for_seconds:.0f}s)",
                    details=vars(health),
                )
            return Check(
                "camera",
                "critical",
                health.detail or "the camera stream is not publishing",
                "Run: sudo ./scripts/diagnose-camera.sh",
                details=vars(health),
            )

        def pir() -> Check:
            status = self.pir.status()
            if not self.config.motion.enabled:
                return Check("pir", UNKNOWN, "motion detection is disabled in the configuration")
            if not status.available:
                return Check(
                    "pir",
                    "critical",
                    status.last_error or "the PIR sensor could not be opened",
                    "The pin is retried every 30s. Run: sudo ./scripts/diagnose-pir.sh",
                    details=vars(status),
                )
            if status.warming_up:
                return Check("pir", DEGRADED, f"warming up, {status.warmup_remaining_seconds:.0f}s left", details=vars(status))
            if status.stuck_suspected:
                return Check(
                    "pir",
                    DEGRADED,
                    "the sensor has reported motion continuously for an implausible time",
                    "Check the wiring, sensitivity and placement of the PIR module.",
                    details=vars(status),
                )
            return Check("pir", OK, f"ready, {status.trigger_count} trigger(s) since start", details=vars(status))

        def arming() -> Check:
            status = self.arming.status()
            if status["armed"]:
                return Check("arming", OK, "armed - motion is recorded", details=status)
            return Check(
                "arming",
                DEGRADED,
                f"disarmed by {status['changed_by']} - motion will not be recorded",
                "Arm the camera from the web UI to start recording motion again.",
                details=status,
            )

        def storage() -> Check:
            status = self.storage.status()
            details = {"free": status.free_bytes, "used_percent": status.used_percent}
            if not status.writable:
                return Check("storage", "critical", "the storage directory is not writable",
                             "Run: sudo ./scripts/diagnose-storage.sh", details)
            if status.critical:
                return Check("storage", "critical", "less than 200 MB of free space remains",
                             "Lower storage.retention_days or free space.", details)
            if status.over_limit:
                return Check("storage", DEGRADED, "above the configured usage limit; old events are being deleted",
                             details=details)
            return Check("storage", OK, f"{status.used_percent}% used", details=details)

        def network() -> Check:
            status = self.network.status()
            if status.online:
                return Check("network", OK, f"online via {status.interface or 'unknown'}", details=vars(status))
            needed = self.config.ai.enabled or self.config.notifications.enabled
            level = DEGRADED if needed else UNKNOWN
            return Check(
                "network",
                level,
                status.detail,
                "AI and notifications are queued and retried automatically." if needed else "",
                details=vars(status),
            )

        def queue() -> Check:
            count = self.pending.pending_count
            if count > 50:
                return Check("queue", DEGRADED, f"{count} events are waiting for AI or notification retries")
            return Check("queue", OK, f"{count} event(s) waiting for retries")

        return {"camera": camera, "pir": pir, "arming": arming, "storage": storage, "network": network, "queue": queue}


def _service_credentials() -> Tuple[str, str]:
    """Internal MediaMTX credentials, taken from the environment or generated per run."""
    user = os.environ.get("SECURECAM_MEDIAMTX_SERVICE_USER", "").strip() or "securecam-service"
    password = os.environ.get("SECURECAM_MEDIAMTX_SERVICE_PASS", "").strip() or secrets.token_urlsafe(32)
    from .logging_setup import register_secret

    register_secret(password)
    return (user, password)


def _notify_systemd(message: str) -> None:
    """Send a readiness or watchdog notification when running under systemd."""
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
    except OSError:
        pass


def _watchdog_interval() -> float:
    """Half the systemd watchdog period, or 0 when the watchdog is off."""
    try:
        usec = int(os.environ.get("WATCHDOG_USEC", "0"))
    except ValueError:
        return 0.0
    return (usec / 1_000_000.0) / 2 if usec > 0 else 0.0


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="securecam", description="Raspberry Pi security camera controller")
    parser.add_argument("--config", default=os.environ.get("SECURECAM_CONFIG", DEFAULT_CONFIG_PATH))
    parser.add_argument("--check", action="store_true", help="validate the configuration and exit")
    parser.add_argument("--version", action="version", version=f"securecam {__version__}")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Process entry point."""
    args = _parse_args(argv)
    load_env_file()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        configure_logging()
        print(exc.render(), file=sys.stderr)
        return 2

    configure_logging(
        level=config.logging.level,
        fmt=config.logging.format,
        file_path=config.logging.file,
        max_bytes=config.logging.max_bytes,
        backup_count=config.logging.backup_count,
    )
    for key in config.unknown_keys:
        log.warning("Ignoring unknown configuration option '%s' - check the spelling", key)

    if args.check:
        controller = Controller(config)
        report = build_report(controller.context(), include_events=False)
        print(format_report(report))
        return 1 if report["warnings"] else 0

    controller = Controller(config)

    def handle_signal(signum, _frame) -> None:
        log.info("Received %s", signal.Signals(signum).name)
        controller.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        controller.start()
    except Exception as exc:
        log.error("Startup failed: %s", exc, exc_info=True)
        controller.stop()
        return 1

    try:
        controller.run()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
