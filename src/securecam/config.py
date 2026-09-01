"""Typed configuration: load, merge with defaults, validate, and report clearly."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

try:  # PyYAML is the only hard third-party dependency of this module.
    import yaml
except ImportError as _exc:  # pragma: no cover - exercised only on a broken install
    raise SystemExit(
        "PyYAML is missing.\n"
        "  What failed: SecureCam could not import the YAML parser.\n"
        "  Why: the virtualenv was created without --system-site-packages, or\n"
        "       python3-yaml is not installed.\n"
        "  Fix:  sudo apt install -y python3-yaml   (or: pip install PyYAML)\n"
    ) from _exc


CONFIG_DIR = os.environ.get("SECURECAM_CONFIG_DIR", "/etc/securecam")
RUNTIME_DIR = os.environ.get("SECURECAM_RUNTIME_DIR", "/run/securecam")
DEFAULT_CONFIG_PATH = os.path.join(CONFIG_DIR, "config.yaml")
USERS_PATH = os.path.join(CONFIG_DIR, "users.json")
SECRET_KEY_PATH = os.path.join(CONFIG_DIR, "secret.key")
DEVICE_ID_PATH = os.path.join(CONFIG_DIR, "device_id")
ENV_FILE_PATH = os.path.join(CONFIG_DIR, "securecam.env")

_TEMPLATE_DIR_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config"),
    "/usr/local/share/securecam/config",
    CONFIG_DIR,
    os.path.join(CONFIG_DIR, "templates"),
)


class ConfigError(Exception):
    """Raised when the configuration cannot be used. Carries every problem found."""

    def __init__(self, problems: Sequence[str], path: str = "") -> None:
        self.problems = list(problems)
        self.path = path
        super().__init__(self.render())

    def render(self) -> str:
        """Format all problems as an actionable multi-line message."""
        where = self.path or "configuration"
        lines = [f"{len(self.problems)} problem(s) found in {where}:"]
        lines += [f"  - {problem}" for problem in self.problems]
        lines.append("")
        lines.append("Nothing was started. Fix the values above and run: securecam-admin check-config")
        return "\n".join(lines)


class _Reader:
    """Reads one config section, collecting type/range problems instead of raising."""

    def __init__(self, data: Any, path: str, problems: List[str], unknown: List[str]) -> None:
        self._data: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
        if data is not None and not isinstance(data, Mapping):
            problems.append(f"{path or 'root'}: expected a mapping, got {type(data).__name__}")
        self._path = path
        self._problems = problems
        self._unknown = unknown
        self._seen: set = set()

    def _label(self, key: str) -> str:
        return f"{self._path}.{key}" if self._path else key

    def raw(self, key: str, default: Any = None) -> Any:
        """Return a value untouched."""
        self._seen.add(key)
        return self._data.get(key, default)

    def string(self, key: str, default: str = "") -> str:
        self._seen.add(key)
        value = self._data.get(key, default)
        if value is None:
            return default
        if isinstance(value, (str, int, float)):
            return str(value)
        self._problems.append(f"{self._label(key)}: expected text, got {type(value).__name__}")
        return default

    def choice(self, key: str, default: str, options: Sequence[str]) -> str:
        value = self.string(key, default)
        if value not in options:
            self._problems.append(f"{self._label(key)}: must be one of {', '.join(options)} (got '{value}')")
            return default
        return value

    def integer(self, key: str, default: int, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
        self._seen.add(key)
        value = self._data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            self._problems.append(f"{self._label(key)}: expected a whole number, got {type(value).__name__}")
            return default
        try:
            result = int(value)
        except (TypeError, ValueError):
            self._problems.append(f"{self._label(key)}: '{value}' is not a whole number")
            return default
        return int(self._check_range(key, result, minimum, maximum, default))

    def number(self, key: str, default: float, minimum: Optional[float] = None, maximum: Optional[float] = None) -> float:
        self._seen.add(key)
        value = self._data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            self._problems.append(f"{self._label(key)}: expected a number, got {type(value).__name__}")
            return default
        try:
            result = float(value)
        except (TypeError, ValueError):
            self._problems.append(f"{self._label(key)}: '{value}' is not a number")
            return default
        return float(self._check_range(key, result, minimum, maximum, default))

    def _check_range(self, key, value, minimum, maximum, default):
        if minimum is not None and value < minimum:
            self._problems.append(f"{self._label(key)}: must be >= {minimum} (got {value})")
            return default
        if maximum is not None and value > maximum:
            self._problems.append(f"{self._label(key)}: must be <= {maximum} (got {value})")
            return default
        return value

    def boolean(self, key: str, default: bool) -> bool:
        self._seen.add(key)
        value = self._data.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("yes", "no", "true", "false", "on", "off"):
            return value.strip().lower() in ("yes", "true", "on")
        self._problems.append(f"{self._label(key)}: expected true or false (got '{value}')")
        return default

    def str_list(self, key: str, default: Optional[List[str]] = None) -> List[str]:
        self._seen.add(key)
        value = self._data.get(key, default if default is not None else [])
        if value is None:
            return list(default or [])
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        self._problems.append(f"{self._label(key)}: expected a list, got {type(value).__name__}")
        return list(default or [])

    def section(self, key: str) -> "_Reader":
        """Descend into a nested mapping."""
        self._seen.add(key)
        return _Reader(self._data.get(key), self._label(key), self._problems, self._unknown)

    def finish(self) -> None:
        """Record any keys we never looked at so typos become visible."""
        for key in self._data:
            if key not in self._seen:
                self._unknown.append(self._label(str(key)))


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DeviceConfig:
    id: str = ""
    name: str = "SecureCam"
    timezone: str = ""


@dataclass
class CameraConfig:
    enabled: bool = True
    width: int = 1920
    height: int = 1080
    fps: int = 15
    bitrate: int = 3_000_000
    idr_period: int = 30
    codec: str = "auto"
    h264_profile: str = "auto"
    h264_level: str = "4.1"
    camera_id: int = 0
    hflip: bool = False
    vflip: bool = False
    denoise: str = "off"
    tuning_file: str = ""
    text_overlay: bool = True
    text_overlay_format: str = "%Y-%m-%d %H:%M:%S"


@dataclass
class MediaMTXConfig:
    binary: str = "/usr/local/bin/mediamtx"
    config_path: str = os.path.join(CONFIG_DIR, "mediamtx.yml")
    path_name: str = "cam"
    log_level: str = "info"
    api_address: str = "127.0.0.1:9997"
    playback_address: str = "127.0.0.1:9996"
    rtsp_address: str = "127.0.0.1:8554"
    hls_address: str = "127.0.0.1:8888"
    webrtc_address: str = "0.0.0.0:8889"
    webrtc_local_udp_address: str = ":8189"
    webrtc_additional_hosts: List[str] = field(default_factory=list)

    def _local(self, address: str) -> str:
        host, _, port = address.rpartition(":")
        if not host or host in ("0.0.0.0", "::", "[::]", ""):
            host = "127.0.0.1"
        return f"{host}:{port}"

    @property
    def api_base_url(self) -> str:
        """Base URL of the MediaMTX control API as reachable from this host."""
        return f"http://{self._local(self.api_address)}"

    @property
    def playback_base_url(self) -> str:
        """Base URL of the MediaMTX playback server as reachable from this host."""
        return f"http://{self._local(self.playback_address)}"

    @property
    def rtsp_url(self) -> str:
        """Local RTSP URL of the camera path, used for snapshots."""
        return f"rtsp://{self._local(self.rtsp_address)}/{self.path_name}"

    @property
    def webrtc_port(self) -> int:
        try:
            return int(self.webrtc_address.rpartition(":")[2])
        except ValueError:
            return 8889


@dataclass
class MotionConfig:
    source: str = "pir"
    gpio: int = 17
    gpio_chip: str = ""
    pull: str = "down"
    active_state: str = "high"
    armed_default: bool = True
    poll_interval_seconds: float = 0.05
    debounce_seconds: float = 0.2
    min_active_seconds: float = 1.0
    warmup_seconds: float = 60.0
    pre_event_seconds: float = 60.0
    post_motion_seconds: float = 300.0
    max_event_seconds: float = 1800.0
    cooldown_seconds: float = 5.0

    @property
    def enabled(self) -> bool:
        return self.source != "disabled"


@dataclass
class StorageConfig:
    base_path: str = "/var/lib/securecam"
    events_path: str = "/var/lib/securecam/events"
    buffer_path: str = "/var/lib/securecam/buffer"
    buffer_retain_minutes: int = 60
    buffer_segment_seconds: int = 60
    retention_days: int = 7
    max_usage_percent: int = 80
    min_free_gb: float = 5.0
    check_interval_seconds: int = 300


@dataclass
class OpenAIVisionConfig:
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key_env: str = "SECURECAM_AI_API_KEY"
    detail: str = "low"
    max_tokens: int = 200


@dataclass
class GenericHTTPAIConfig:
    endpoint: str = ""
    api_key_env: str = "SECURECAM_AI_API_KEY"
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    encoding: str = "multipart"
    field_name: str = "image"
    person_field: str = "person_detected"
    confidence_field: str = "confidence"
    label_field: str = "label"


@dataclass
class AIConfig:
    enabled: bool = False
    provider: str = "openai_vision"
    timeout_seconds: float = 20.0
    snapshot_count: int = 1
    snapshot_interval_seconds: float = 2.0
    min_confidence: float = 0.6
    recheck_interval_seconds: float = 60.0
    max_checks: int = 4
    max_retry_age_hours: float = 24.0
    openai_vision: OpenAIVisionConfig = field(default_factory=OpenAIVisionConfig)
    generic_http: GenericHTTPAIConfig = field(default_factory=GenericHTTPAIConfig)


@dataclass
class NtfyConfig:
    server: str = "https://ntfy.sh"
    token_env: str = "SECURECAM_NTFY_TOKEN"
    priority: int = 5
    tags: List[str] = field(default_factory=lambda: ["rotating_light"])
    call: str = ""


@dataclass
class PushoverConfig:
    api_url: str = "https://api.pushover.net/1/messages.json"
    token_env: str = "SECURECAM_PUSHOVER_TOKEN"
    priority: int = 2
    retry_seconds: int = 30
    expire_seconds: int = 600
    sound: str = "persistent"


@dataclass
class RecipientConfig:
    id: str = ""
    enabled: bool = False
    provider: str = ""
    target: str = ""
    alarm: bool = True


@dataclass
class NotificationsConfig:
    enabled: bool = False
    provider: str = "ntfy"
    alarm: bool = True
    include_snapshot: bool = True
    only_if_person: bool = False
    cooldown_seconds: float = 0.0
    max_retry_age_hours: float = 24.0
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    pushover: PushoverConfig = field(default_factory=PushoverConfig)
    recipients: List[RecipientConfig] = field(default_factory=list)

    def active_recipients(self) -> List[RecipientConfig]:
        """Recipients that are switched on and have a usable target."""
        return [r for r in self.recipients if r.enabled and r.target and r.target != "CHANGE-ME"]


@dataclass
class TLSConfig:
    enabled: bool = False
    cert_file: str = ""
    key_file: str = ""


@dataclass
class RateLimitConfig:
    login_attempts: int = 10
    window_seconds: int = 300


@dataclass
class APIConfig:
    enabled: bool = True
    address: str = "0.0.0.0"
    port: int = 8080
    # Localhost-only listener that answers MediaMTX authentication callbacks.
    # Kept separate so the callback is never reachable from the network and so
    # it stays plain HTTP even when the public API uses TLS.
    internal_auth_port: int = 9095
    web_ui: bool = True
    session_ttl_hours: float = 12.0
    stream_ticket_ttl_seconds: int = 120
    public_base_url: str = ""
    tls: TLSConfig = field(default_factory=TLSConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)


@dataclass
class NetworkConfig:
    check_interval_seconds: int = 60
    retry_initial_seconds: float = 10.0
    retry_max_seconds: float = 300.0
    internet_probe_hosts: List[str] = field(default_factory=lambda: ["1.1.1.1:443", "8.8.8.8:443"])
    dns_probe_host: str = "cloudflare.com"
    probe_timeout_seconds: float = 4.0


@dataclass
class HealthConfig:
    interval_seconds: int = 30
    state_file: str = os.path.join(RUNTIME_DIR, "health.json")
    cpu_temp_warn_celsius: float = 75.0
    cpu_temp_critical_celsius: float = 80.0
    disk_warn_percent: int = 80


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "text"
    file: str = ""
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 3


@dataclass
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    mediamtx: MediaMTXConfig = field(default_factory=MediaMTXConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    api: APIConfig = field(default_factory=APIConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    source_path: str = ""
    unknown_keys: List[str] = field(default_factory=list)

    @property
    def buffer_retain_seconds(self) -> float:
        return self.storage.buffer_retain_minutes * 60.0

    def template_path(self, name: str) -> str:
        """Locate a packaged template file, searching repo and installed locations."""
        for directory in _TEMPLATE_DIR_CANDIDATES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
        raise ConfigError(
            [
                f"template '{name}' was not found in any of: {', '.join(_TEMPLATE_DIR_CANDIDATES)}. "
                "Reinstall with install.sh, which copies the templates to "
                "/usr/local/share/securecam/config."
            ]
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_config(path: Optional[str] = None, strict_unknown: bool = False) -> Config:
    """Read, parse and validate a config file. Raises ConfigError with every problem."""
    resolved = path or os.environ.get("SECURECAM_CONFIG", DEFAULT_CONFIG_PATH)
    if not os.path.isfile(resolved):
        raise ConfigError(
            [
                f"config file '{resolved}' does not exist. "
                "Copy config/config.example.yaml to it, or point SECURECAM_CONFIG at another file."
            ],
            resolved,
        )
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError([f"YAML syntax error: {exc}"], resolved) from exc
    except OSError as exc:
        raise ConfigError([f"cannot read config file: {exc}"], resolved) from exc

    if raw is None:
        raw = {}
    config = parse_config(raw, resolved, strict_unknown=strict_unknown)
    return config


def parse_config(raw: Mapping[str, Any], source_path: str = "", strict_unknown: bool = False) -> Config:
    """Turn a plain mapping into a validated Config."""
    problems: List[str] = []
    unknown: List[str] = []
    root = _Reader(raw, "", problems, unknown)

    config = Config(source_path=source_path)
    _read_device(root.section("device"), config.device)
    _read_camera(root.section("camera"), config.camera)
    _read_mediamtx(root.section("mediamtx"), config.mediamtx)
    _read_motion(root.section("motion"), config.motion)
    _read_storage(root.section("storage"), config.storage)
    _read_ai(root.section("ai"), config.ai)
    _read_notifications(root.section("notifications"), config.notifications, problems, unknown)
    _read_api(root.section("api"), config.api)
    _read_network(root.section("network"), config.network)
    _read_health(root.section("health"), config.health)
    _read_logging(root.section("logging"), config.logging)
    root.finish()

    _cross_validate(config, problems)
    config.unknown_keys = unknown

    if strict_unknown and unknown:
        problems.extend(
            f"unknown option '{key}' - check the spelling against config/config.example.yaml" for key in unknown
        )
    if problems:
        raise ConfigError(problems, source_path)
    return config


def _read_device(reader: _Reader, target: DeviceConfig) -> None:
    target.id = reader.string("id", target.id).strip()
    target.name = reader.string("name", target.name).strip()
    target.timezone = reader.string("timezone", target.timezone).strip()
    reader.finish()


def _read_camera(reader: _Reader, target: CameraConfig) -> None:
    target.enabled = reader.boolean("enabled", target.enabled)
    target.width = reader.integer("width", target.width, 160, 4096)
    target.height = reader.integer("height", target.height, 120, 2464)
    target.fps = reader.integer("fps", target.fps, 1, 60)
    target.bitrate = reader.integer("bitrate", target.bitrate, 100_000, 25_000_000)
    target.idr_period = reader.integer("idr_period", target.idr_period, 1, 600)
    target.codec = reader.choice("codec", target.codec, ("auto", "hardwareH264", "softwareH264"))
    target.h264_profile = reader.choice("h264_profile", target.h264_profile, ("auto", "baseline", "main", "high"))
    target.h264_level = reader.choice("h264_level", target.h264_level, ("4", "4.1", "4.2"))
    target.camera_id = reader.integer("camera_id", target.camera_id, 0, 3)
    target.hflip = reader.boolean("hflip", target.hflip)
    target.vflip = reader.boolean("vflip", target.vflip)
    target.denoise = reader.choice("denoise", target.denoise, ("off", "cdn_off", "cdn_fast", "cdn_hq"))
    target.tuning_file = reader.string("tuning_file", target.tuning_file).strip()
    target.text_overlay = reader.boolean("text_overlay", target.text_overlay)
    target.text_overlay_format = reader.string("text_overlay_format", target.text_overlay_format)
    reader.finish()


def _read_mediamtx(reader: _Reader, target: MediaMTXConfig) -> None:
    target.binary = reader.string("binary", target.binary)
    target.config_path = reader.string("config_path", target.config_path)
    target.path_name = reader.string("path_name", target.path_name).strip()
    target.log_level = reader.choice("log_level", target.log_level, ("error", "warn", "info", "debug"))
    target.api_address = reader.string("api_address", target.api_address)
    target.playback_address = reader.string("playback_address", target.playback_address)
    target.rtsp_address = reader.string("rtsp_address", target.rtsp_address)
    target.hls_address = reader.string("hls_address", target.hls_address)
    target.webrtc_address = reader.string("webrtc_address", target.webrtc_address)
    target.webrtc_local_udp_address = reader.string("webrtc_local_udp_address", target.webrtc_local_udp_address)
    target.webrtc_additional_hosts = reader.str_list("webrtc_additional_hosts", target.webrtc_additional_hosts)
    reader.finish()


def _read_motion(reader: _Reader, target: MotionConfig) -> None:
    target.source = reader.choice("source", target.source, ("pir", "disabled"))
    target.gpio = reader.integer("gpio", target.gpio, 0, 27)
    target.gpio_chip = reader.string("gpio_chip", target.gpio_chip).strip()
    target.pull = reader.choice("pull", target.pull, ("down", "up", "none"))
    target.active_state = reader.choice("active_state", target.active_state, ("high", "low"))
    target.armed_default = reader.boolean("armed_default", target.armed_default)
    target.poll_interval_seconds = reader.number("poll_interval_seconds", target.poll_interval_seconds, 0.005, 1.0)
    target.debounce_seconds = reader.number("debounce_seconds", target.debounce_seconds, 0.0, 10.0)
    target.min_active_seconds = reader.number("min_active_seconds", target.min_active_seconds, 0.0, 120.0)
    target.warmup_seconds = reader.number("warmup_seconds", target.warmup_seconds, 0.0, 600.0)
    target.pre_event_seconds = reader.number("pre_event_seconds", target.pre_event_seconds, 0.0, 900.0)
    target.post_motion_seconds = reader.number("post_motion_seconds", target.post_motion_seconds, 1.0, 3600.0)
    target.max_event_seconds = reader.number("max_event_seconds", target.max_event_seconds, 10.0, 21600.0)
    target.cooldown_seconds = reader.number("cooldown_seconds", target.cooldown_seconds, 0.0, 600.0)
    reader.finish()


def _read_storage(reader: _Reader, target: StorageConfig) -> None:
    target.base_path = reader.string("base_path", target.base_path)
    target.events_path = reader.string("events_path", target.events_path)
    target.buffer_path = reader.string("buffer_path", target.buffer_path)
    target.buffer_retain_minutes = reader.integer("buffer_retain_minutes", target.buffer_retain_minutes, 2, 1440)
    target.buffer_segment_seconds = reader.integer("buffer_segment_seconds", target.buffer_segment_seconds, 10, 600)
    target.retention_days = reader.integer("retention_days", target.retention_days, 1, 3650)
    target.max_usage_percent = reader.integer("max_usage_percent", target.max_usage_percent, 10, 99)
    target.min_free_gb = reader.number("min_free_gb", target.min_free_gb, 0.5, 1000.0)
    target.check_interval_seconds = reader.integer("check_interval_seconds", target.check_interval_seconds, 30, 86400)
    reader.finish()


def _read_ai(reader: _Reader, target: AIConfig) -> None:
    target.enabled = reader.boolean("enabled", target.enabled)
    target.provider = reader.choice("provider", target.provider, ("disabled", "openai_vision", "generic_http"))
    target.timeout_seconds = reader.number("timeout_seconds", target.timeout_seconds, 1.0, 300.0)
    target.snapshot_count = reader.integer("snapshot_count", target.snapshot_count, 1, 5)
    target.snapshot_interval_seconds = reader.number("snapshot_interval_seconds", target.snapshot_interval_seconds, 0.0, 60.0)
    target.min_confidence = reader.number("min_confidence", target.min_confidence, 0.0, 1.0)
    target.recheck_interval_seconds = reader.number("recheck_interval_seconds", target.recheck_interval_seconds, 0.0, 3600.0)
    target.max_checks = reader.integer("max_checks", target.max_checks, 1, 20)
    target.max_retry_age_hours = reader.number("max_retry_age_hours", target.max_retry_age_hours, 0.0, 720.0)

    openai = reader.section("openai_vision")
    target.openai_vision.base_url = openai.string("base_url", target.openai_vision.base_url).rstrip("/")
    target.openai_vision.model = openai.string("model", target.openai_vision.model)
    target.openai_vision.api_key_env = openai.string("api_key_env", target.openai_vision.api_key_env)
    target.openai_vision.detail = openai.choice("detail", target.openai_vision.detail, ("low", "high", "auto"))
    target.openai_vision.max_tokens = openai.integer("max_tokens", target.openai_vision.max_tokens, 16, 4096)
    openai.finish()

    generic = reader.section("generic_http")
    target.generic_http.endpoint = generic.string("endpoint", target.generic_http.endpoint).strip()
    target.generic_http.api_key_env = generic.string("api_key_env", target.generic_http.api_key_env)
    target.generic_http.auth_header = generic.string("auth_header", target.generic_http.auth_header)
    target.generic_http.auth_scheme = generic.string("auth_scheme", target.generic_http.auth_scheme)
    target.generic_http.encoding = generic.choice("encoding", target.generic_http.encoding, ("multipart", "json_base64"))
    target.generic_http.field_name = generic.string("field_name", target.generic_http.field_name)
    target.generic_http.person_field = generic.string("person_field", target.generic_http.person_field)
    target.generic_http.confidence_field = generic.string("confidence_field", target.generic_http.confidence_field)
    target.generic_http.label_field = generic.string("label_field", target.generic_http.label_field)
    generic.finish()

    reader.finish()


def _read_notifications(
    reader: _Reader, target: NotificationsConfig, problems: List[str], unknown: List[str]
) -> None:
    target.enabled = reader.boolean("enabled", target.enabled)
    target.provider = reader.choice("provider", target.provider, ("disabled", "ntfy", "pushover"))
    target.alarm = reader.boolean("alarm", target.alarm)
    target.include_snapshot = reader.boolean("include_snapshot", target.include_snapshot)
    target.only_if_person = reader.boolean("only_if_person", target.only_if_person)
    target.cooldown_seconds = reader.number("cooldown_seconds", target.cooldown_seconds, 0.0, 86400.0)
    target.max_retry_age_hours = reader.number("max_retry_age_hours", target.max_retry_age_hours, 0.0, 720.0)

    ntfy = reader.section("ntfy")
    target.ntfy.server = ntfy.string("server", target.ntfy.server).rstrip("/")
    target.ntfy.token_env = ntfy.string("token_env", target.ntfy.token_env)
    target.ntfy.priority = ntfy.integer("priority", target.ntfy.priority, 1, 5)
    target.ntfy.tags = ntfy.str_list("tags", target.ntfy.tags)
    target.ntfy.call = ntfy.string("call", target.ntfy.call).strip()
    ntfy.finish()

    pushover = reader.section("pushover")
    target.pushover.api_url = pushover.string("api_url", target.pushover.api_url)
    target.pushover.token_env = pushover.string("token_env", target.pushover.token_env)
    target.pushover.priority = pushover.integer("priority", target.pushover.priority, -2, 2)
    target.pushover.retry_seconds = pushover.integer("retry_seconds", target.pushover.retry_seconds, 30, 10800)
    target.pushover.expire_seconds = pushover.integer("expire_seconds", target.pushover.expire_seconds, 30, 10800)
    target.pushover.sound = pushover.string("sound", target.pushover.sound)
    pushover.finish()

    raw_recipients = reader.raw("recipients", [])
    recipients: List[RecipientConfig] = []
    if raw_recipients is None:
        raw_recipients = []
    if not isinstance(raw_recipients, list):
        problems.append("notifications.recipients: expected a list of recipients")
        raw_recipients = []
    seen_ids = set()
    for index, entry in enumerate(raw_recipients):
        sub = _Reader(entry, f"notifications.recipients[{index}]", problems, unknown)
        recipient = RecipientConfig()
        recipient.id = sub.string("id", f"recipient{index + 1}").strip()
        recipient.enabled = sub.boolean("enabled", False)
        recipient.provider = sub.choice("provider", "", ("", "disabled", "ntfy", "pushover"))
        recipient.target = sub.string("target", "").strip()
        recipient.alarm = sub.boolean("alarm", True)
        sub.finish()
        if recipient.id in seen_ids:
            problems.append(f"notifications.recipients[{index}].id: '{recipient.id}' is used more than once")
        seen_ids.add(recipient.id)
        recipients.append(recipient)
    target.recipients = recipients
    reader.finish()


def _read_api(reader: _Reader, target: APIConfig) -> None:
    target.enabled = reader.boolean("enabled", target.enabled)
    target.address = reader.string("address", target.address)
    target.port = reader.integer("port", target.port, 1, 65535)
    target.internal_auth_port = reader.integer("internal_auth_port", target.internal_auth_port, 1, 65535)
    target.web_ui = reader.boolean("web_ui", target.web_ui)
    target.session_ttl_hours = reader.number("session_ttl_hours", target.session_ttl_hours, 0.1, 8760.0)
    target.stream_ticket_ttl_seconds = reader.integer(
        "stream_ticket_ttl_seconds", target.stream_ticket_ttl_seconds, 10, 3600
    )
    target.public_base_url = reader.string("public_base_url", target.public_base_url).rstrip("/")

    tls = reader.section("tls")
    target.tls.enabled = tls.boolean("enabled", target.tls.enabled)
    target.tls.cert_file = tls.string("cert_file", target.tls.cert_file)
    target.tls.key_file = tls.string("key_file", target.tls.key_file)
    tls.finish()

    limits = reader.section("rate_limit")
    target.rate_limit.login_attempts = limits.integer("login_attempts", target.rate_limit.login_attempts, 1, 1000)
    target.rate_limit.window_seconds = limits.integer("window_seconds", target.rate_limit.window_seconds, 10, 86400)
    limits.finish()

    reader.finish()


def _read_network(reader: _Reader, target: NetworkConfig) -> None:
    target.check_interval_seconds = reader.integer("check_interval_seconds", target.check_interval_seconds, 5, 3600)
    target.retry_initial_seconds = reader.number("retry_initial_seconds", target.retry_initial_seconds, 1.0, 3600.0)
    target.retry_max_seconds = reader.number("retry_max_seconds", target.retry_max_seconds, 1.0, 86400.0)
    target.internet_probe_hosts = reader.str_list("internet_probe_hosts", target.internet_probe_hosts)
    target.dns_probe_host = reader.string("dns_probe_host", target.dns_probe_host)
    target.probe_timeout_seconds = reader.number("probe_timeout_seconds", target.probe_timeout_seconds, 0.5, 60.0)
    reader.finish()


def _read_health(reader: _Reader, target: HealthConfig) -> None:
    target.interval_seconds = reader.integer("interval_seconds", target.interval_seconds, 5, 3600)
    target.state_file = reader.string("state_file", target.state_file)
    target.cpu_temp_warn_celsius = reader.number("cpu_temp_warn_celsius", target.cpu_temp_warn_celsius, 30.0, 100.0)
    target.cpu_temp_critical_celsius = reader.number(
        "cpu_temp_critical_celsius", target.cpu_temp_critical_celsius, 30.0, 110.0
    )
    target.disk_warn_percent = reader.integer("disk_warn_percent", target.disk_warn_percent, 10, 99)
    reader.finish()


def _read_logging(reader: _Reader, target: LoggingConfig) -> None:
    target.level = reader.choice("level", target.level, ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
    target.format = reader.choice("format", target.format, ("text", "json"))
    target.file = reader.string("file", target.file).strip()
    target.max_bytes = reader.integer("max_bytes", target.max_bytes, 65536, 1024 * 1024 * 1024)
    target.backup_count = reader.integer("backup_count", target.backup_count, 0, 100)
    reader.finish()


def _cross_validate(config: Config, problems: List[str]) -> None:
    """Checks that involve more than one section."""
    for label, path in (
        ("storage.base_path", config.storage.base_path),
        ("storage.events_path", config.storage.events_path),
        ("storage.buffer_path", config.storage.buffer_path),
    ):
        if not os.path.isabs(path):
            problems.append(f"{label}: must be an absolute path (got '{path}')")

    if os.path.normpath(config.storage.events_path) == os.path.normpath(config.storage.buffer_path):
        problems.append(
            "storage.events_path and storage.buffer_path must differ - the rolling buffer is deleted "
            "automatically and would take saved events with it"
        )

    required_buffer = config.motion.pre_event_seconds + config.motion.max_event_seconds + 120
    if config.motion.enabled and config.buffer_retain_seconds < required_buffer:
        problems.append(
            f"storage.buffer_retain_minutes: {config.storage.buffer_retain_minutes} min is too short. "
            f"It must cover motion.pre_event_seconds + motion.max_event_seconds plus 2 min of slack "
            f"({required_buffer / 60:.0f} min). Increase it, or lower pre_event_seconds/max_event_seconds."
        )

    if config.motion.max_event_seconds <= config.motion.post_motion_seconds:
        problems.append(
            "motion.max_event_seconds must be larger than motion.post_motion_seconds, otherwise every "
            "event is cut off before its quiet period ends"
        )

    if config.motion.min_active_seconds < config.motion.debounce_seconds:
        problems.append("motion.min_active_seconds must be >= motion.debounce_seconds")

    if config.camera.width % 2 or config.camera.height % 2:
        problems.append("camera.width and camera.height must both be even numbers for H.264")

    if config.camera.idr_period > config.camera.fps * 4:
        problems.append(
            f"camera.idr_period ({config.camera.idr_period}) is more than 4 seconds of video at "
            f"{config.camera.fps} fps. Event clips could start several seconds late. "
            f"Use {config.camera.fps * 2} or less."
        )

    if config.camera.tuning_file and not os.path.isabs(config.camera.tuning_file):
        problems.append("camera.tuning_file: must be an absolute path")

    if config.storage.buffer_segment_seconds > config.storage.buffer_retain_minutes * 60 / 2:
        problems.append("storage.buffer_segment_seconds must be at most half of storage.buffer_retain_minutes")

    if config.ai.enabled:
        if config.ai.provider == "disabled":
            problems.append("ai.enabled is true but ai.provider is 'disabled' - pick a real provider or turn ai off")
        if config.ai.provider == "generic_http" and not config.ai.generic_http.endpoint:
            problems.append("ai.generic_http.endpoint is required when ai.provider is 'generic_http'")
        if config.ai.provider == "openai_vision" and not config.ai.openai_vision.base_url:
            problems.append("ai.openai_vision.base_url is required when ai.provider is 'openai_vision'")

    if config.notifications.enabled:
        if config.notifications.provider == "disabled" and not any(
            r.provider for r in config.notifications.recipients
        ):
            problems.append(
                "notifications.enabled is true but no provider is selected - set notifications.provider "
                "to ntfy or pushover"
            )
        if not config.notifications.active_recipients():
            problems.append(
                "notifications.enabled is true but no recipient is enabled with a real target. "
                "Set notifications.recipients[].enabled: true and replace the CHANGE-ME target."
            )

    if config.notifications.only_if_person and not config.ai.enabled:
        problems.append(
            "notifications.only_if_person requires ai.enabled: true, otherwise no notification would ever be sent"
        )

    if config.api.tls.enabled:
        for label, path in (("api.tls.cert_file", config.api.tls.cert_file), ("api.tls.key_file", config.api.tls.key_file)):
            if not path:
                problems.append(f"{label} is required when api.tls.enabled is true")
            elif not os.path.isfile(path):
                problems.append(f"{label}: '{path}' does not exist")

    used = {
        "mediamtx.webrtc_address": config.mediamtx.webrtc_port,
        "mediamtx.api_address": _port_of(config.mediamtx.api_address),
        "mediamtx.playback_address": _port_of(config.mediamtx.playback_address),
        "mediamtx.rtsp_address": _port_of(config.mediamtx.rtsp_address),
        "mediamtx.hls_address": _port_of(config.mediamtx.hls_address),
    }
    if config.api.enabled:
        used["api.port"] = config.api.port
        used["api.internal_auth_port"] = config.api.internal_auth_port
    seen: Dict[int, str] = {}
    for label, port in used.items():
        if port <= 0:
            continue
        if port in seen:
            problems.append(f"{label} and {seen[port]} both use TCP port {port}; give each service its own port")
        seen[port] = label

    if config.network.retry_max_seconds < config.network.retry_initial_seconds:
        problems.append("network.retry_max_seconds must be >= network.retry_initial_seconds")

    if config.health.cpu_temp_critical_celsius <= config.health.cpu_temp_warn_celsius:
        problems.append("health.cpu_temp_critical_celsius must be greater than health.cpu_temp_warn_celsius")

    if not config.mediamtx.path_name.isidentifier() and not config.mediamtx.path_name.replace("-", "_").isidentifier():
        problems.append(
            f"mediamtx.path_name '{config.mediamtx.path_name}' should contain only letters, digits, '-' and '_'"
        )


def redacted_dict(config: Config) -> Dict[str, Any]:
    """Config as plain data for the API, with anything secret-adjacent removed."""
    from dataclasses import asdict

    data = asdict(config)
    data.pop("source_path", None)
    for recipient in data.get("notifications", {}).get("recipients", []):
        if recipient.get("target"):
            recipient["target"] = _mask(recipient["target"])
    return data


def _port_of(address: str) -> int:
    """Extract the TCP port from a host:port string, or 0 when unparsable."""
    try:
        return int(address.rpartition(":")[2])
    except ValueError:
        return 0


def _mask(value: str) -> str:
    """Show only enough of an identifier to recognise it."""
    if len(value) <= 4:
        return "****"
    return value[:2] + "*" * (len(value) - 4) + value[-2:]
