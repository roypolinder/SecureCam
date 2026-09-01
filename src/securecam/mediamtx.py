"""MediaMTX integration: config rendering, control API, playback extraction, supervision."""

from __future__ import annotations

import base64
import os
import string
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import Config
from .logging_setup import get_logger
from .util import (
    HttpError,
    atomic_write_bytes,
    http_download,
    http_request,
    monotonic,
    run_command,
    to_rfc3339,
    which,
)

log = get_logger("mediamtx")

SERVICE_UNIT = "securecam-mediamtx.service"


# ---------------------------------------------------------------------------
# Configuration rendering
# ---------------------------------------------------------------------------


def render_config(config: Config, service_user: str, service_password: str) -> str:
    """Render mediamtx.yml from the template using the SecureCam configuration."""
    template_path = config.template_path("mediamtx.template.yml")
    with open(template_path, "r", encoding="utf-8") as handle:
        template = string.Template(handle.read())

    if config.api.enabled:
        auth_block = "\n".join(
            [
                "authMethod: http",
                f"authHTTPAddress: http://127.0.0.1:{config.api.internal_auth_port}/internal/mediamtx/auth",
                "authHTTPExclude: []",
            ]
        )
    else:
        auth_block = "\n".join(
            [
                "authMethod: internal",
                "authInternalUsers:",
                f"  - user: {service_user}",
                f"    pass: {service_password}",
                "    ips: ['127.0.0.1', '::1']",
                "    permissions:",
                "      - action: read",
                "      - action: playback",
                "      - action: api",
            ]
        )

    record_path = os.path.join(config.storage.buffer_path, "%path", "%Y-%m-%d_%H-%M-%S-%f")
    profile = "main" if config.camera.h264_profile == "auto" else config.camera.h264_profile

    values: Dict[str, str] = {
        "LOG_LEVEL": config.mediamtx.log_level,
        "AUTH_BLOCK": auth_block,
        "API_ADDRESS": config.mediamtx.api_address,
        "PLAYBACK_ADDRESS": config.mediamtx.playback_address,
        "RTSP_ENABLED": "yes",
        "RTSP_ADDRESS": config.mediamtx.rtsp_address,
        "WEBRTC_ENABLED": _yes(config.api.enabled),
        "WEBRTC_ADDRESS": config.mediamtx.webrtc_address,
        "WEBRTC_UDP_ADDRESS": config.mediamtx.webrtc_local_udp_address,
        "WEBRTC_ADDITIONAL_HOSTS": "[" + ", ".join(f"'{_quote(h)}'" for h in config.mediamtx.webrtc_additional_hosts) + "]",
        "HLS_ENABLED": "no",
        "HLS_ADDRESS": config.mediamtx.hls_address,
        "PATH_NAME": config.mediamtx.path_name,
        "CAM_ID": str(config.camera.camera_id),
        "CAM_WIDTH": str(config.camera.width),
        "CAM_HEIGHT": str(config.camera.height),
        "CAM_FPS": str(config.camera.fps),
        "CAM_HFLIP": _yes(config.camera.hflip),
        "CAM_VFLIP": _yes(config.camera.vflip),
        "CAM_DENOISE": config.camera.denoise,
        "CAM_CODEC": config.camera.codec,
        "CAM_BITRATE": str(config.camera.bitrate),
        "CAM_PROFILE": profile,
        "CAM_LEVEL": config.camera.h264_level,
        "CAM_IDR_PERIOD": str(config.camera.idr_period),
        "CAM_TUNING_FILE": _quote(config.camera.tuning_file),
        "CAM_OVERLAY_ENABLE": _yes(config.camera.text_overlay),
        "CAM_OVERLAY_FORMAT": _quote(config.camera.text_overlay_format),
        "RECORD_ENABLED": _yes(config.camera.enabled),
        "RECORD_PATH": record_path,
        "RECORD_SEGMENT_DURATION": f"{config.storage.buffer_segment_seconds}s",
        "RECORD_DELETE_AFTER": f"{config.storage.buffer_retain_minutes}m",
    }
    rendered = template.safe_substitute(values)
    missing = [name for name in values if "${" + name + "}" in rendered]
    if missing:  # pragma: no cover - only reachable if the template is edited badly
        raise ValueError(f"mediamtx template still contains unsubstituted placeholders: {missing}")
    return rendered


def write_config(config: Config, service_user: str, service_password: str) -> bool:
    """Write mediamtx.yml if the content changed. Returns True when it was rewritten."""
    rendered = render_config(config, service_user, service_password).encode("utf-8")
    target = config.mediamtx.config_path
    try:
        with open(target, "rb") as handle:
            if handle.read() == rendered:
                return False
    except OSError:
        pass
    atomic_write_bytes(target, rendered, mode=0o640)
    log.info("Wrote MediaMTX configuration to %s", target)
    return True


def _yes(value: bool) -> str:
    """YAML-safe boolean the way MediaMTX writes them."""
    return "yes" if value else "no"


def _quote(value: str) -> str:
    """Escape a value that is embedded inside single quotes in YAML."""
    return str(value).replace("'", "''")


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


@dataclass
class PathStatus:
    name: str = ""
    ready: bool = False
    ready_since: Optional[str] = None
    tracks: List[str] = field(default_factory=list)
    bytes_received: int = 0
    readers: int = 0
    source_type: str = ""
    error: str = ""


@dataclass
class Segment:
    start: datetime
    duration: float


class MediaMTXClient:
    """Talks to the MediaMTX control API and playback server over localhost."""

    def __init__(self, config: Config, service_user: str, service_password: str, timeout: float = 10.0) -> None:
        self._config = config
        self._user = service_user
        self._password = service_password
        self._timeout = timeout
        self._use_query_auth = False

    # -- control API --------------------------------------------------------

    def path_status(self, name: Optional[str] = None) -> PathStatus:
        """Current state of the camera path, including whether it is publishing."""
        path_name = name or self._config.mediamtx.path_name
        url = f"{self._config.mediamtx.api_base_url}/v3/paths/get/{urllib.parse.quote(path_name)}"
        try:
            payload = self._get_json(url)
        except HttpError as exc:
            if exc.status == 404:
                return PathStatus(name=path_name, ready=False, error="path not found in MediaMTX")
            return PathStatus(name=path_name, ready=False, error=str(exc))
        except Exception as exc:  # the control API must never take down the caller
            return PathStatus(name=path_name, ready=False, error=f"control API request failed: {exc}")
        tracks = payload.get("tracks") or []
        return PathStatus(
            name=path_name,
            ready=bool(payload.get("ready")),
            ready_since=payload.get("readyTime"),
            tracks=[str(track) for track in tracks],
            bytes_received=int(payload.get("bytesReceived") or 0),
            readers=len(payload.get("readers") or []),
            source_type=str((payload.get("source") or {}).get("type", "")),
        )

    def alive(self) -> bool:
        """True when the MediaMTX control API answers."""
        try:
            self._get_json(f"{self._config.mediamtx.api_base_url}/v3/paths/list?itemsPerPage=1")
            return True
        except HttpError:
            return False

    # -- playback -----------------------------------------------------------

    def list_segments(self, start: datetime, end: datetime, name: Optional[str] = None) -> List[Segment]:
        """Recorded timespans available in the rolling buffer for a window."""
        path_name = name or self._config.mediamtx.path_name
        query = urllib.parse.urlencode(
            {"path": path_name, "start": to_rfc3339(start), "end": to_rfc3339(end)}
        )
        url = f"{self._config.mediamtx.playback_base_url}/list?{query}"
        payload = self._get_json(url)
        segments: List[Segment] = []
        if isinstance(payload, list):
            for entry in payload:
                try:
                    from .util import parse_rfc3339

                    segments.append(Segment(parse_rfc3339(entry["start"]), float(entry["duration"])))
                except (KeyError, TypeError, ValueError):
                    continue
        return segments

    def download_clip(
        self,
        destination: str,
        start: datetime,
        duration: float,
        name: Optional[str] = None,
        fmt: str = "mp4",
        timeout: float = 300.0,
    ) -> int:
        """Extract a time range from the rolling buffer without re-encoding."""
        path_name = name or self._config.mediamtx.path_name
        params = {
            "path": path_name,
            "start": to_rfc3339(start),
            "duration": f"{max(1.0, duration):.3f}",
            "format": fmt,
        }
        base = f"{self._config.mediamtx.playback_base_url}/get"
        url = f"{base}?{urllib.parse.urlencode(params)}"
        try:
            return http_download(url, destination, headers=self._auth_headers(), timeout=timeout)
        except HttpError as exc:
            if exc.status in (401, 403):
                params.update({"user": self._user, "pass": self._password})
                retry_url = f"{base}?{urllib.parse.urlencode(params)}"
                self._use_query_auth = True
                return http_download(retry_url, destination, timeout=timeout)
            raise

    # -- internals ----------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        token = base64.b64encode(f"{self._user}:{self._password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _get_json(self, url: str) -> Any:
        """GET JSON with Basic auth, falling back to query credentials if rejected."""
        if self._use_query_auth:
            url = _with_credentials(url, self._user, self._password)
            return http_request(url, timeout=self._timeout).json()
        try:
            return http_request(url, headers=self._auth_headers(), timeout=self._timeout).json()
        except HttpError as exc:
            if exc.status in (401, 403):
                self._use_query_auth = True
                return http_request(
                    _with_credentials(url, self._user, self._password), timeout=self._timeout
                ).json()
            raise


def _with_credentials(url: str, user: str, password: str) -> str:
    """Append user/pass query parameters for MediaMTX endpoints that need them."""
    separator = "&" if "?" in url else "?"
    return url + separator + urllib.parse.urlencode({"user": user, "pass": password})


# ---------------------------------------------------------------------------
# Supervision
# ---------------------------------------------------------------------------


@dataclass
class StreamHealth:
    probed: bool = False
    api_reachable: bool = False
    path_ready: bool = False
    readers: int = 0
    bytes_received: int = 0
    tracks: List[str] = field(default_factory=list)
    unhealthy_for_seconds: float = 0.0
    restart_attempts: int = 0
    last_restart_at: Optional[str] = None
    detail: str = ""


class MediaMTXSupervisor:
    """Watches the stream and restarts the MediaMTX unit when it stops producing video."""

    def __init__(self, config: Config, client: MediaMTXClient, restart_after_seconds: float = 90.0) -> None:
        self._config = config
        self._client = client
        self._restart_after = restart_after_seconds
        self._unhealthy_since: Optional[float] = None
        self._lock = threading.Lock()
        self._health = StreamHealth()
        self._logged_failure = False
        self._min_restart_interval = 300.0
        self._last_restart = 0.0

    def health(self) -> StreamHealth:
        """Latest observed stream health."""
        with self._lock:
            return StreamHealth(**vars(self._health))

    def check(self, allow_restart: bool = True) -> StreamHealth:
        """Probe MediaMTX once and act on sustained failure."""
        now = monotonic()
        status = self._client.path_status()
        api_ok = status.error == "" or "path not found" in status.error
        healthy = status.ready

        with self._lock:
            self._health.probed = True
            self._health.api_reachable = api_ok
            self._health.path_ready = status.ready
            self._health.readers = status.readers
            self._health.bytes_received = status.bytes_received
            self._health.tracks = status.tracks
            self._health.detail = status.error

        if healthy:
            if self._unhealthy_since is not None:
                log.info("Camera stream recovered after %.0fs", now - self._unhealthy_since)
            self._unhealthy_since = None
            self._logged_failure = False
            with self._lock:
                self._health.unhealthy_for_seconds = 0.0
            return self.health()

        if self._unhealthy_since is None:
            self._unhealthy_since = now
        outage = now - self._unhealthy_since
        with self._lock:
            self._health.unhealthy_for_seconds = round(outage, 1)

        if not self._logged_failure and outage > 10:
            self._logged_failure = True
            log.error(
                "The camera stream is not publishing (%s).\n"
                "  What still works: the API, stored events and notifications. Nothing new is being recorded.\n"
                "  Likely causes: the camera ribbon is loose, the camera is disabled in raspi-config, another\n"
                "  process holds the camera, or MediaMTX failed to start.\n"
                "  Diagnose: sudo ./scripts/diagnose-camera.sh\n"
                "  Logs: journalctl -u %s -n 50 --no-pager",
                status.error or "path not ready",
                SERVICE_UNIT,
            )

        if allow_restart and outage >= self._restart_after and (now - self._last_restart) >= self._min_restart_interval:
            self._last_restart = now
            self.restart_unit()
        return self.health()

    def restart_unit(self) -> None:
        """Ask systemd to restart MediaMTX. Requires the sudoers rule from install.sh."""
        with self._lock:
            self._health.restart_attempts += 1
            from .util import utcnow

            self._health.last_restart_at = to_rfc3339(utcnow())
        log.warning("Restarting %s after a sustained stream outage", SERVICE_UNIT)
        command = ["systemctl", "restart", SERVICE_UNIT]
        if os.geteuid() != 0 and which("sudo"):
            command = ["sudo", "-n"] + command
        result = run_command(command, timeout=30)
        if not result.ok:
            log.error(
                "Automatic restart of %s failed: %s\n"
                "  Why: the service user is not allowed to run systemctl for this unit.\n"
                "  Fix: reinstall (install.sh adds the required sudoers rule) or restart it yourself with\n"
                "       sudo systemctl restart %s",
                SERVICE_UNIT,
                (result.stderr or result.stdout).strip()[:200],
                SERVICE_UNIT,
            )
