"""Small shared helpers: time, atomic writes, HTTP, backoff, formatting."""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

UTC = timezone.utc


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def to_rfc3339(value: datetime) -> str:
    """Format an aware datetime as RFC3339 with milliseconds, as MediaMTX expects."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC3339 timestamp, tolerating a trailing Z and fractional seconds."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_duration_seconds(value: Any) -> float:
    """Accept 90, '90', '90s', '5m', '2h', '1d' and return seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        raise ValueError("empty duration")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text)


def format_duration(seconds: float) -> str:
    """Render seconds as a compact human string such as '2h 5m 3s'."""
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def human_bytes(num: float) -> str:
    """Render a byte count using binary units."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0 or unit == "TiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} TiB"


# --------------------------------------------------------------------------
# Filesystem
# --------------------------------------------------------------------------


def ensure_dir(path: str, mode: int = 0o750) -> str:
    """Create a directory tree if missing and return the path."""
    os.makedirs(path, mode=mode, exist_ok=True)
    return path


def atomic_write_bytes(path: str, data: bytes, mode: int = 0o640) -> None:
    """Write a file so readers never observe a partial or truncated version."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    ensure_dir(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, payload: Any, mode: int = 0o640) -> None:
    """Atomically write pretty-printed JSON."""
    data = json.dumps(payload, indent=2, sort_keys=False, default=str).encode("utf-8")
    atomic_write_bytes(path, data + b"\n", mode=mode)


def read_json(path: str) -> Any:
    """Read a JSON file, raising ValueError with the path on malformed content."""
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc


def _fsync_dir(path: str) -> None:
    """Flush a directory entry so a rename survives power loss."""
    try:
        fd = os.open(path, os.O_DIRECTORY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def dir_size_bytes(path: str) -> int:
    """Total size of a directory tree, ignoring files that vanish mid-scan."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


# --------------------------------------------------------------------------
# Mappings
# --------------------------------------------------------------------------


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursively merge override into base without mutating either."""
    result: Dict[str, Any] = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


def dotted_get(payload: Any, path: str, default: Any = None) -> Any:
    """Look up 'a.b.c' inside nested dicts/lists, returning default when absent."""
    current = payload
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return default
            current = current[index]
        else:
            return default
    return current


# --------------------------------------------------------------------------
# Retry / backoff
# --------------------------------------------------------------------------


class Backoff:
    """Exponential backoff with a ceiling and small jitter, used for every retry loop."""

    def __init__(self, initial: float, maximum: float, factor: float = 2.0, jitter: float = 0.1) -> None:
        self.initial = max(0.1, float(initial))
        self.maximum = max(self.initial, float(maximum))
        self.factor = max(1.0, float(factor))
        self.jitter = max(0.0, float(jitter))
        self._current = 0.0
        self.attempts = 0

    def reset(self) -> None:
        """Forget previous failures."""
        self._current = 0.0
        self.attempts = 0

    def next_delay(self) -> float:
        """Return the delay to wait before the next attempt."""
        self.attempts += 1
        self._current = self.initial if self._current <= 0 else min(self._current * self.factor, self.maximum)
        if self.jitter:
            spread = self._current * self.jitter
            return max(0.1, self._current - spread + secrets.randbelow(1000) / 1000.0 * 2 * spread)
        return self._current

    @property
    def current(self) -> float:
        """The delay produced by the last call to next_delay (0 before the first)."""
        return self._current


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class HttpError(Exception):
    """Raised for transport failures and non-2xx responses."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    @property
    def retryable(self) -> bool:
        """True when retrying later could plausibly succeed."""
        if self.status is None:
            return True
        if self.status in (408, 425, 429):
            return True
        return self.status >= 500


@dataclass
class HttpResponse:
    status: int
    headers: Dict[str, str]
    body: bytes

    def json(self) -> Any:
        """Decode the body as JSON."""
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[Mapping[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: float = 15.0,
    max_body_bytes: int = 8 * 1024 * 1024,
) -> HttpResponse:
    """Perform an HTTP request with the stdlib and normalize every failure to HttpError."""
    request = urllib.request.Request(url, data=data, method=method.upper())
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    context = ssl.create_default_context() if url.lower().startswith("https") else None
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = response.read(max_body_bytes + 1)
            if len(body) > max_body_bytes:
                raise HttpError(f"response from {_safe_url(url)} exceeded {max_body_bytes} bytes")
            return HttpResponse(response.status, dict(response.headers.items()), body)
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        raise HttpError(
            f"{method.upper()} {_safe_url(url)} failed with HTTP {exc.code}",
            status=exc.code,
            body=detail,
        ) from exc
    except urllib.error.URLError as exc:
        raise HttpError(f"{method.upper()} {_safe_url(url)} failed: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise HttpError(f"{method.upper()} {_safe_url(url)} timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise HttpError(f"{method.upper()} {_safe_url(url)} failed: {exc}") from exc


def http_download(
    url: str,
    destination: str,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 120.0,
    max_bytes: int = 4 * 1024 * 1024 * 1024,
) -> int:
    """Stream a response body straight to disk and return the number of bytes written."""
    request = urllib.request.Request(url, method="GET")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    ensure_dir(os.path.dirname(os.path.abspath(destination)))
    partial = destination + ".part"
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, open(partial, "wb") as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HttpError(f"download from {_safe_url(url)} exceeded {max_bytes} bytes")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except urllib.error.HTTPError as exc:
        _unlink_quiet(partial)
        detail = exc.read(2048).decode("utf-8", errors="replace")
        raise HttpError(f"GET {_safe_url(url)} failed with HTTP {exc.code}", status=exc.code, body=detail) from exc
    except urllib.error.URLError as exc:
        _unlink_quiet(partial)
        raise HttpError(f"GET {_safe_url(url)} failed: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        _unlink_quiet(partial)
        raise HttpError(f"GET {_safe_url(url)} timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        _unlink_quiet(partial)
        raise HttpError(f"GET {_safe_url(url)} failed: {exc}") from exc
    if written == 0:
        _unlink_quiet(partial)
        raise HttpError(f"GET {_safe_url(url)} returned an empty body")
    os.replace(partial, destination)
    return written


def encode_multipart(
    fields: Mapping[str, str],
    files: Iterable[Tuple[str, str, bytes]] = (),
) -> Tuple[bytes, str]:
    """Build a multipart/form-data body and return it with its Content-Type."""
    boundary = "----securecam" + secrets.token_hex(16)
    buffer = bytearray()
    for name, value in fields.items():
        buffer += f"--{boundary}\r\n".encode()
        buffer += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        buffer += str(value).encode("utf-8") + b"\r\n"
    for name, filename, content in files:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        buffer += f"--{boundary}\r\n".encode()
        buffer += f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        buffer += f"Content-Type: {content_type}\r\n\r\n".encode()
        buffer += content + b"\r\n"
    buffer += f"--{boundary}--\r\n".encode()
    return bytes(buffer), f"multipart/form-data; boundary={boundary}"


def _safe_url(url: str) -> str:
    """Strip credentials and query strings so URLs can be logged safely."""
    try:
        parts = urllib.parse.urlsplit(url)
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return "<malformed url>"


def _unlink_quiet(path: str) -> None:
    """Delete a file, ignoring the case where it is already gone."""
    try:
        os.unlink(path)
    except OSError:
        pass


def tcp_probe(host: str, port: int, timeout: float = 4.0) -> bool:
    """Return True when a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Subprocess
# --------------------------------------------------------------------------


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_command(args: Sequence[str], timeout: float = 20.0, stdin: Optional[bytes] = None) -> CommandResult:
    """Run a command without a shell and never raise on failure."""
    try:
        completed = subprocess.run(  # noqa: S603 - argument list is built in code, never from user input
            list(args),
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(127, "", f"{args[0]}: command not found")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{args[0]}: timed out after {timeout:.0f}s", timed_out=True)
    except OSError as exc:
        return CommandResult(126, "", f"{args[0]}: {exc}")
    return CommandResult(
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )


def which(name: str) -> Optional[str]:
    """Locate an executable on PATH."""
    from shutil import which as _which

    return _which(name)


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------


def new_id(nbytes: int = 4) -> str:
    """Short random hex suffix used to make event ids unique."""
    return secrets.token_hex(nbytes)


def clamp(value: float, low: float, high: float) -> float:
    """Constrain a number to an inclusive range."""
    return max(low, min(high, value))


def monotonic() -> float:
    """Monotonic clock, isolated here so tests can patch it."""
    return time.monotonic()


def expires_at(seconds: float) -> datetime:
    """Absolute UTC deadline a given number of seconds from now."""
    return utcnow() + timedelta(seconds=seconds)
