"""PIR motion sensor: GPIO backends, glitch filtering and a polling monitor thread."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .config import MotionConfig
from .logging_setup import get_logger
from .util import monotonic, utcnow

log = get_logger("pir")


class MotionEdge(Enum):
    START = "start"
    END = "end"


class SensorError(Exception):
    """Raised when the GPIO backend cannot be opened or read."""


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class SensorBackend:
    """Reads the current logical motion state of a sensor."""

    def read(self) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        """Release the GPIO line."""

    def describe(self) -> str:
        return self.__class__.__name__


class MockSensor(SensorBackend):
    """In-memory sensor used by tests and by `motion.source: disabled`."""

    def __init__(self, initial: bool = False) -> None:
        self.state = initial
        self.closed = False

    def read(self) -> bool:
        return self.state

    def set(self, value: bool) -> None:
        """Drive the simulated sensor line."""
        self.state = bool(value)

    def close(self) -> None:
        self.closed = True

    def describe(self) -> str:
        return "mock sensor"


class GpiozeroSensor(SensorBackend):
    """Real PIR input backed by gpiozero on the Linux GPIO character device."""

    def __init__(self, config: MotionConfig) -> None:
        self._config = config
        try:
            from gpiozero import DigitalInputDevice  # type: ignore import-not-found
        except ImportError as exc:
            raise SensorError(
                "gpiozero is not installed, so the PIR sensor cannot be read.\n"
                "  Why: the package is missing, or the virtualenv was created without "
                "--system-site-packages so it cannot see the apt-installed copy.\n"
                "  Everything else (streaming, API) still works, but no motion will ever be detected.\n"
                "  Fix: sudo apt install -y python3-gpiozero python3-lgpio && sudo systemctl restart securecam"
            ) from exc

        factory = self._make_pin_factory(config.gpio_chip)
        kwargs = {}
        if factory is not None:
            kwargs["pin_factory"] = factory

        pull = config.pull
        try:
            if pull == "none":
                self._device = DigitalInputDevice(
                    config.gpio, pull_up=None, active_state=(config.active_state == "high"), **kwargs
                )
            else:
                self._device = DigitalInputDevice(config.gpio, pull_up=(pull == "up"), **kwargs)
        except Exception as exc:  # gpiozero raises a wide family of errors here
            raise SensorError(
                f"Cannot open GPIO{config.gpio} for the PIR sensor: {exc}\n"
                "  Why it probably failed: the pin is already used by another process or by a device-tree\n"
                "  overlay, the service user is not in the 'gpio' group, or the pin number is wrong.\n"
                "  Diagnose: sudo ./scripts/diagnose-pir.sh\n"
                "  Fix: pick a free pin with motion.gpio in /etc/securecam/config.yaml, or free the pin."
            ) from exc

        self._invert = pull == "up"
        self._active_high = config.active_state == "high"

    @staticmethod
    def _make_pin_factory(chip: str):
        """Pin the sensor to a specific gpiochip when the user asked for one."""
        if not chip:
            return None
        try:
            from gpiozero.pins.lgpio import LGPIOFactory  # type: ignore import-not-found
        except ImportError as exc:
            raise SensorError(
                "motion.gpio_chip is set but the lgpio backend is not installed.\n"
                "  Fix: sudo apt install -y python3-lgpio, or clear motion.gpio_chip to use the default backend."
            ) from exc
        number = "".join(char for char in chip if char.isdigit())
        try:
            return LGPIOFactory(chip=int(number) if number else 0)
        except Exception as exc:
            raise SensorError(
                f"Cannot open GPIO chip '{chip}': {exc}\n"
                "  List the chips available on this Pi with: ls -l /dev/gpiochip*"
            ) from exc

    def read(self) -> bool:
        try:
            raw = bool(self._device.value)
        except Exception as exc:
            raise SensorError(f"reading GPIO{self._config.gpio} failed: {exc}") from exc
        line_high = (not raw) if self._invert else raw
        return line_high if self._active_high else (not line_high)

    def close(self) -> None:
        try:
            self._device.close()
        except Exception:  # closing must never mask the original shutdown reason
            pass

    def describe(self) -> str:
        return f"gpiozero GPIO{self._config.gpio} pull={self._config.pull} active={self._config.active_state}"


def create_sensor(config: MotionConfig) -> SensorBackend:
    """Build the sensor backend selected by configuration."""
    if config.source == "disabled":
        return MockSensor()
    return GpiozeroSensor(config)


# ---------------------------------------------------------------------------
# Debouncing
# ---------------------------------------------------------------------------


class MotionDebouncer:
    """Turns a noisy PIR line into clean start/end edges. Pure logic, no I/O."""

    def __init__(self, debounce_seconds: float, min_active_seconds: float) -> None:
        self.debounce_seconds = max(0.0, float(debounce_seconds))
        self.min_active_seconds = max(0.0, float(min_active_seconds))
        self._stable = False
        self._candidate = False
        self._candidate_since = 0.0
        self._active_since: Optional[float] = None

    @property
    def active(self) -> bool:
        """The debounced state."""
        return self._stable

    @property
    def active_since(self) -> Optional[float]:
        return self._active_since

    def update(self, raw: bool, now: float) -> Optional[MotionEdge]:
        """Feed one sample and return an edge when the debounced state changes."""
        raw = bool(raw)
        if raw != self._candidate:
            self._candidate = raw
            self._candidate_since = now
        if self._candidate == self._stable:
            return None
        if now - self._candidate_since < self.debounce_seconds:
            return None
        if (
            not self._candidate
            and self._active_since is not None
            and now - self._active_since < self.min_active_seconds
        ):
            # Hold the active state a little longer; PIR modules dip low mid-motion.
            return None
        self._stable = self._candidate
        if self._stable:
            self._active_since = now
            return MotionEdge.START
        self._active_since = None
        return MotionEdge.END


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


@dataclass
class PirStatus:
    available: bool = False
    active: bool = False
    warming_up: bool = True
    warmup_remaining_seconds: float = 0.0
    trigger_count: int = 0
    last_trigger: Optional[str] = None
    last_error: str = ""
    consecutive_errors: int = 0
    description: str = ""
    stuck_suspected: bool = False
    extra: dict = field(default_factory=dict)


class PirMonitor:
    """Polls the sensor on a thread and reports debounced motion edges."""

    def __init__(
        self,
        config: MotionConfig,
        on_edge: Callable[[MotionEdge, float], None],
        sensor: Optional[SensorBackend] = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._config = config
        self._on_edge = on_edge
        self._clock = clock
        self._sensor = sensor
        self._debouncer = MotionDebouncer(config.debounce_seconds, config.min_active_seconds)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status = PirStatus()
        self._started_at = 0.0
        self._warmup_logged = False
        self._stuck_threshold = max(config.max_event_seconds * 2, 3600.0)

    def start(self) -> None:
        """Open the sensor and begin polling. Never raises; degrades instead."""
        if self._config.source == "disabled":
            log.warning("motion.source is 'disabled' - no motion will ever be recorded")
            with self._lock:
                self._status.description = "disabled by configuration"
            return
        self._started_at = self._clock()
        if self._sensor is None:
            try:
                self._sensor = create_sensor(self._config)
            except SensorError as exc:
                with self._lock:
                    self._status.available = False
                    self._status.last_error = str(exc).splitlines()[0]
                    self._status.description = "unavailable"
                log.error("%s", exc)
                return
        with self._lock:
            self._status.available = True
            self._status.description = self._sensor.describe()
        log.info(
            "PIR monitor started on %s, warming up for %.0fs", self._sensor.describe(), self._config.warmup_seconds
        )
        self._thread = threading.Thread(target=self._run, name="securecam-pir", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop polling and release the GPIO line."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._sensor is not None:
            self._sensor.close()

    def status(self) -> PirStatus:
        """Snapshot of sensor health for /api/health and diagnostics."""
        with self._lock:
            remaining = max(0.0, self._config.warmup_seconds - (self._clock() - self._started_at))
            self._status.warming_up = remaining > 0 and self._status.available
            self._status.warmup_remaining_seconds = round(remaining, 1)
            self._status.active = self._debouncer.active
            return PirStatus(**vars(self._status))

    def _run(self) -> None:
        interval = self._config.poll_interval_seconds
        while not self._stop.wait(interval):
            self.poll_once()

    def poll_once(self) -> None:
        """Read the sensor once and dispatch any resulting edge."""
        now = self._clock()
        try:
            raw = self._sensor.read()  # type: ignore[union-attr]
        except SensorError as exc:
            self._record_error(str(exc))
            return
        with self._lock:
            if self._status.consecutive_errors:
                log.info("PIR sensor recovered after %d failed reads", self._status.consecutive_errors)
            self._status.consecutive_errors = 0
            self._status.last_error = ""

        edge = self._debouncer.update(raw, now)
        if (now - self._started_at) < self._config.warmup_seconds:
            return
        if not self._warmup_logged:
            self._warmup_logged = True
            log.info("PIR warm-up finished, motion detection is live")

        self._check_stuck(now)
        if edge is None:
            return
        if edge is MotionEdge.START:
            with self._lock:
                self._status.trigger_count += 1
                self._status.last_trigger = utcnow().isoformat()
        try:
            self._on_edge(edge, now)
        except Exception:
            log.exception("Motion handler raised on %s; motion detection continues", edge.value)

    def _check_stuck(self, now: float) -> None:
        """Flag a sensor that has been continuously active for an implausible time."""
        since = self._debouncer.active_since
        stuck = since is not None and (now - since) > self._stuck_threshold
        with self._lock:
            if stuck and not self._status.stuck_suspected:
                log.warning(
                    "PIR on GPIO%d has reported motion continuously for over %.0f minutes. "
                    "The sensor may be miswired, saturated by heat, or set to an extreme sensitivity. "
                    "Recording still works but events will be capped at motion.max_event_seconds. "
                    "Diagnose with: sudo ./scripts/diagnose-pir.sh",
                    self._config.gpio,
                    self._stuck_threshold / 60,
                )
            self._status.stuck_suspected = stuck

    def _record_error(self, message: str) -> None:
        """Track read failures and log the first one plus periodic reminders."""
        with self._lock:
            self._status.consecutive_errors += 1
            self._status.last_error = message
            count = self._status.consecutive_errors
        if count == 1 or count % 200 == 0:
            log.error(
                "Cannot read the PIR sensor (%d consecutive failures): %s. "
                "Motion detection is down; streaming and the API are unaffected. "
                "Diagnose with: sudo ./scripts/diagnose-pir.sh",
                count,
                message,
            )
