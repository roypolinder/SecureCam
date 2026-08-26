"""System and subsystem health: what works, what does not, and what to do about it."""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .logging_setup import get_logger
from .util import atomic_write_json, ensure_dir, format_duration, human_bytes, run_command, to_rfc3339, utcnow, which

log = get_logger("health")

OK = "ok"
DEGRADED = "degraded"
CRITICAL = "critical"
UNKNOWN = "unknown"

_SEVERITY = {OK: 0, UNKNOWN: 1, DEGRADED: 2, CRITICAL: 3}

THROTTLE_BITS = {
    0: ("under_voltage_now", "The power supply cannot keep up right now"),
    1: ("arm_frequency_capped_now", "The CPU is frequency-capped right now"),
    2: ("throttled_now", "The CPU is being throttled right now"),
    3: ("soft_temp_limit_now", "The soft temperature limit is active right now"),
    16: ("under_voltage_occurred", "The power supply dropped below 4.63 V at some point"),
    17: ("arm_frequency_capped_occurred", "The CPU was frequency-capped at some point"),
    18: ("throttled_occurred", "The CPU was throttled at some point"),
    19: ("soft_temp_limit_occurred", "The soft temperature limit was reached at some point"),
}


@dataclass
class Check:
    name: str
    status: str = UNKNOWN
    message: str = ""
    remedy: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemInfo:
    cpu_temp_celsius: Optional[float] = None
    cpu_load_1m: Optional[float] = None
    memory_total_bytes: int = 0
    memory_available_bytes: int = 0
    memory_used_percent: float = 0.0
    uptime_seconds: float = 0.0
    throttling: Dict[str, bool] = field(default_factory=dict)
    kernel: str = ""
    model: str = ""


def read_cpu_temperature() -> Optional[float]:
    """CPU temperature in Celsius from the thermal zone, or None off-Pi."""
    for path in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return round(int(handle.read().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            continue
    return None


def read_load_average() -> Optional[float]:
    """One-minute load average."""
    try:
        return round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        return None


def read_memory() -> Dict[str, int]:
    """Total and available memory in bytes from /proc/meminfo."""
    values: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                number = rest.strip().split(" ")[0]
                if number.isdigit():
                    values[key] = int(number) * 1024
    except OSError:
        return {}
    return {"total": values.get("MemTotal", 0), "available": values.get("MemAvailable", 0)}


def read_uptime() -> float:
    """System uptime in seconds."""
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def read_model() -> str:
    """Raspberry Pi model string."""
    try:
        with open("/proc/device-tree/model", "rb") as handle:
            return handle.read().decode("utf-8", errors="replace").strip("\x00").strip()
    except OSError:
        return ""


def read_throttling() -> Dict[str, bool]:
    """Decode `vcgencmd get_throttled` into named flags."""
    if not which("vcgencmd"):
        return {}
    result = run_command(["vcgencmd", "get_throttled"], timeout=5)
    if not result.ok or "=" not in result.stdout:
        return {}
    try:
        value = int(result.stdout.strip().split("=")[1], 16)
    except (ValueError, IndexError):
        return {}
    return {name: bool(value & (1 << bit)) for bit, (name, _) in THROTTLE_BITS.items()}


def collect_system_info() -> SystemInfo:
    """Everything about the host that matters for a fanless Pi."""
    memory = read_memory()
    total = memory.get("total", 0)
    available = memory.get("available", 0)
    return SystemInfo(
        cpu_temp_celsius=read_cpu_temperature(),
        cpu_load_1m=read_load_average(),
        memory_total_bytes=total,
        memory_available_bytes=available,
        memory_used_percent=round((1 - available / total) * 100, 1) if total else 0.0,
        uptime_seconds=read_uptime(),
        throttling=read_throttling(),
        kernel=os.uname().release if hasattr(os, "uname") else "",
        model=read_model(),
    )


class HealthMonitor:
    """Aggregates subsystem status into one report and publishes it to a state file."""

    def __init__(self, config: Config, providers: Dict[str, Callable[[], Check]]) -> None:
        self._config = config
        self._providers = providers
        self._lock = threading.Lock()
        self._report: Dict[str, Any] = {"status": UNKNOWN, "checks": [], "generated_at": ""}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_status = UNKNOWN

    def report(self) -> Dict[str, Any]:
        """Most recent report."""
        with self._lock:
            return dict(self._report)

    def collect(self) -> Dict[str, Any]:
        """Run every check and build the report."""
        checks: List[Check] = []
        for name, provider in self._providers.items():
            try:
                checks.append(provider())
            except Exception as exc:  # a broken check must never take the service down
                log.exception("Health check '%s' raised", name)
                checks.append(Check(name=name, status=UNKNOWN, message=f"check failed: {exc}"))

        system = collect_system_info()
        checks.append(self._temperature_check(system))
        checks.append(self._power_check(system))
        checks.append(self._memory_check(system))

        overall = OK
        for check in checks:
            if _SEVERITY.get(check.status, 1) > _SEVERITY[overall]:
                overall = check.status

        report = {
            "status": overall,
            "device_id": self._config.device.id,
            "device_name": self._config.device.name,
            "generated_at": to_rfc3339(utcnow()),
            "uptime": format_duration(system.uptime_seconds),
            "system": asdict(system),
            "checks": [asdict(check) for check in checks],
        }
        with self._lock:
            self._report = report
        self._publish(report)
        if overall != self._last_status:
            problems = "; ".join(f"{c.name}: {c.message}" for c in checks if c.status not in (OK, UNKNOWN))
            if overall == OK:
                log.info("Overall health is OK")
            else:
                log.warning("Overall health is %s - %s", overall, problems or "see /api/health")
            self._last_status = overall
        return report

    def start(self) -> None:
        """Begin periodic collection."""
        self._thread = threading.Thread(target=self._run, name="securecam-health", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop periodic collection."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.collect()
            except Exception:
                log.exception("Health collection failed")
            self._stop.wait(self._config.health.interval_seconds)

    def _publish(self, report: Dict[str, Any]) -> None:
        """Write the report where scripts and other services can read it."""
        path = self._config.health.state_file
        if not path:
            return
        try:
            ensure_dir(os.path.dirname(os.path.abspath(path)), mode=0o755)
            atomic_write_json(path, report, mode=0o644)
        except OSError as exc:
            log.debug("Could not write the health state file %s: %s", path, exc)

    def _temperature_check(self, system: SystemInfo) -> Check:
        """A fanless Pi 4 must stay below the throttling threshold."""
        settings = self._config.health
        if system.cpu_temp_celsius is None:
            return Check("temperature", UNKNOWN, "CPU temperature is not readable on this host")
        temp = system.cpu_temp_celsius
        details = {"celsius": temp, "warn_at": settings.cpu_temp_warn_celsius}
        if temp >= settings.cpu_temp_critical_celsius:
            return Check(
                "temperature",
                CRITICAL,
                f"CPU is at {temp} C, at or above the critical threshold",
                "This Pi has no fan. Improve airflow, remove the case lid, add a heatsink, or lower "
                "camera.fps/camera.width. Sustained throttling will drop frames.",
                details,
            )
        if temp >= settings.cpu_temp_warn_celsius:
            return Check(
                "temperature",
                DEGRADED,
                f"CPU is at {temp} C, above the warning threshold",
                "Add a heatsink or improve airflow before it reaches the throttling point.",
                details,
            )
        return Check("temperature", OK, f"CPU is at {temp} C", details=details)

    def _power_check(self, system: SystemInfo) -> Check:
        """Under-voltage is the most common cause of mysterious Pi failures."""
        flags = system.throttling
        if not flags:
            return Check("power", UNKNOWN, "vcgencmd is unavailable, so power state cannot be read")
        if flags.get("under_voltage_now") or flags.get("throttled_now"):
            return Check(
                "power",
                CRITICAL,
                "The Pi is under-volted or throttled right now",
                "Use the official 5 V / 3 A USB-C supply and a short, thick cable. Under-voltage corrupts "
                "SD cards and makes the camera drop out at random.",
                dict(flags),
            )
        if flags.get("under_voltage_occurred") or flags.get("throttled_occurred"):
            return Check(
                "power",
                DEGRADED,
                "Under-voltage or throttling happened since boot",
                "Replace the power supply or cable. Clear the flag by rebooting after fixing it.",
                dict(flags),
            )
        return Check("power", OK, "Power and clocks are nominal", details=dict(flags))

    def _memory_check(self, system: SystemInfo) -> Check:
        """4 GB is plenty for this design; running out means something is leaking."""
        if not system.memory_total_bytes:
            return Check("memory", UNKNOWN, "memory information is not readable")
        details = {
            "total": human_bytes(system.memory_total_bytes),
            "available": human_bytes(system.memory_available_bytes),
            "used_percent": system.memory_used_percent,
        }
        if system.memory_used_percent >= 92:
            return Check(
                "memory",
                CRITICAL,
                f"{system.memory_used_percent}% of RAM is in use",
                "Check for other services on this Pi. SecureCam itself should stay well under 200 MB.",
                details,
            )
        if system.memory_used_percent >= 80:
            return Check("memory", DEGRADED, f"{system.memory_used_percent}% of RAM is in use", details=details)
        return Check("memory", OK, f"{human_bytes(system.memory_available_bytes)} of RAM available", details=details)
