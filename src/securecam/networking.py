"""Connectivity monitoring that tells apart 'no LAN', 'no DNS' and 'no internet'."""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional

from .config import NetworkConfig
from .logging_setup import get_logger
from .util import Backoff, tcp_probe, utcnow

log = get_logger("network")


@dataclass
class ConnectivityStatus:
    link_up: bool = False
    default_route: bool = False
    dns_ok: bool = False
    internet_ok: bool = False
    local_ip: str = ""
    interface: str = ""
    wifi_signal_dbm: Optional[float] = None
    wifi_quality_percent: Optional[float] = None
    checked_at: str = ""
    detail: str = ""

    @property
    def online(self) -> bool:
        """True only when outbound requests to the internet can be expected to work."""
        return self.internet_ok and self.dns_ok

    def describe(self) -> str:
        """Explain the first thing that is broken, in the order the user should fix it."""
        if not self.link_up:
            return "no network interface is up"
        if not self.default_route:
            return "the interface is up but there is no default gateway"
        if not self.internet_ok:
            return "the gateway is reachable but the internet is not"
        if not self.dns_ok:
            return "the internet is reachable but DNS lookups fail"
        return "online"


def read_default_route() -> Optional[str]:
    """Return the interface that owns the IPv4 default route, or None."""
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as handle:
            for line in handle.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    return fields[0]
    except OSError:
        return None
    return None


def read_local_ip() -> str:
    """Discover the source address used for outbound traffic without sending anything."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.settimeout(1.0)
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def read_wifi_signal() -> tuple:
    """Read (signal_dbm, quality_percent) from /proc/net/wireless, or (None, None)."""
    try:
        with open("/proc/net/wireless", "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return (None, None)
    for line in lines[2:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            quality = float(parts[2].rstrip("."))
            level = float(parts[3].rstrip("."))
        except ValueError:
            continue
        return (level, round(min(100.0, quality / 70.0 * 100.0), 1))
    return (None, None)


class NetworkMonitor:
    """Periodically classifies connectivity and notifies listeners when it changes."""

    def __init__(self, config: NetworkConfig, on_change: Optional[Callable[[ConnectivityStatus], None]] = None) -> None:
        self._config = config
        self._on_change = on_change
        self._status = ConnectivityStatus()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._backoff = Backoff(config.retry_initial_seconds, config.retry_max_seconds)
        self._last_summary = ""

    def set_on_change(self, callback: Optional[Callable[[ConnectivityStatus], None]]) -> None:
        """Register the listener called whenever connectivity flips."""
        self._on_change = callback

    def status(self) -> ConnectivityStatus:
        """Latest classification without probing."""
        with self._lock:
            return ConnectivityStatus(**vars(self._status))

    @property
    def online(self) -> bool:
        return self.status().online

    def check(self) -> ConnectivityStatus:
        """Probe once and update the cached status."""
        status = ConnectivityStatus(checked_at=utcnow().isoformat())
        status.interface = read_default_route() or ""
        status.default_route = bool(status.interface)
        status.local_ip = read_local_ip()
        status.link_up = bool(status.local_ip)
        status.wifi_signal_dbm, status.wifi_quality_percent = read_wifi_signal()

        if status.default_route:
            status.internet_ok = self._probe_internet()
        if status.internet_ok:
            status.dns_ok = self._probe_dns()
        status.detail = status.describe()

        with self._lock:
            previous = self._status
            self._status = status

        if status.detail != self._last_summary:
            self._last_summary = status.detail
            if status.online:
                log.info(
                    "Network is online via %s (%s%s)",
                    status.interface or "unknown interface",
                    status.local_ip or "no address",
                    f", wifi {status.wifi_signal_dbm:.0f} dBm" if status.wifi_signal_dbm is not None else "",
                )
            else:
                log.warning(
                    "Network degraded: %s.\n"
                    "  What still works: motion detection, recording and local streaming are unaffected.\n"
                    "  What is paused: AI analysis and notifications; they are queued and retried automatically.\n"
                    "  Diagnose: sudo ./scripts/diagnose-network.sh",
                    status.detail,
                )
        if self._on_change is not None and previous.online != status.online:
            try:
                self._on_change(status)
            except Exception:
                log.exception("Connectivity change handler failed")
        return status

    def start(self) -> None:
        """Begin background probing."""
        self._thread = threading.Thread(target=self._run, name="securecam-network", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop background probing."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                status = self.check()
            except Exception:
                log.exception("Connectivity check failed unexpectedly")
                status = ConnectivityStatus()
            if status.online:
                self._backoff.reset()
                delay = self._config.check_interval_seconds
            else:
                delay = min(self._backoff.next_delay(), float(self._config.check_interval_seconds))
            self._stop.wait(delay)

    def _probe_internet(self) -> bool:
        """TCP-connect to the configured probe hosts; the first success wins."""
        for entry in self._config.internet_probe_hosts:
            host, _, port = entry.rpartition(":")
            if not host:
                host, port = entry, "443"
            try:
                port_number = int(port)
            except ValueError:
                continue
            if tcp_probe(host, port_number, self._config.probe_timeout_seconds):
                return True
        return False

    def _probe_dns(self) -> bool:
        """Resolve a well-known name to separate DNS failures from routing failures."""
        if not self._config.dns_probe_host:
            return True
        try:
            socket.setdefaulttimeout(self._config.probe_timeout_seconds)
            socket.getaddrinfo(self._config.dns_probe_host, 443, proto=socket.IPPROTO_TCP)
            return True
        except (socket.gaierror, OSError):
            return False
        finally:
            socket.setdefaulttimeout(None)


def resolve_hostnames() -> List[str]:
    """Best-effort list of addresses this device is reachable on, for the setup summary."""
    names: List[str] = []
    local = read_local_ip()
    if local:
        names.append(local)
    try:
        hostname = socket.gethostname()
        if hostname:
            names.append(f"{hostname}.local")
    except OSError:
        pass
    return names
