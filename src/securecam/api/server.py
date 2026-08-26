"""Threaded HTTP server, routing, authentication and static file serving.

Two listeners are started:
  * the public one serves the API and the optional web UI, with TLS when configured;
  * a loopback-only one answers MediaMTX authentication callbacks, so that endpoint
    is never reachable from the network and stays plain HTTP even when TLS is on.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..appcontext import AppContext
from ..auth import Permission, has_permission
from ..logging_setup import get_logger

log = get_logger("api")

MAX_BODY_BYTES = 1 * 1024 * 1024
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SESSION_COOKIE = "securecam_session"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cache-Control": "no-store",
}

# The UI is a single static page that talks to this API and to MediaMTX over
# WebRTC. No third-party origin is ever needed.
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "script-src 'self'; style-src 'self'; connect-src *; frame-ancestors 'none'; base-uri 'none'"
)


class HttpProblem(Exception):
    """Raised inside handlers to return a specific status with a helpful message."""

    def __init__(self, status: int, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.hint = hint


@dataclass
class Session:
    user_id: str
    username: str
    role: str
    device_id: str
    expires_at: float

    def allows(self, permission: Permission) -> bool:
        """True when the session's role grants a permission."""
        return has_permission(self.role, permission)


@dataclass
class Request:
    method: str
    path: str
    query: Dict[str, List[str]]
    headers: Dict[str, str]
    body: bytes
    client_ip: str
    params: Dict[str, str] = field(default_factory=dict)

    def json(self) -> Dict[str, Any]:
        """Parse the body as a JSON object."""
        if not self.body:
            return {}
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpProblem(400, f"the request body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise HttpProblem(400, "the request body must be a JSON object")
        return payload

    def param(self, name: str, default: str = "") -> str:
        """First value of a query parameter."""
        values = self.query.get(name)
        return values[0] if values else default

    def int_param(self, name: str, default: int, minimum: int = 0, maximum: int = 10_000) -> int:
        """Query parameter parsed as a bounded integer."""
        try:
            return max(minimum, min(maximum, int(self.param(name, str(default)))))
        except ValueError:
            return default


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    headers: Dict[str, str] = field(default_factory=dict)
    file_path: str = ""

    @classmethod
    def json(cls, payload: Any, status: int = 200) -> "Response":
        """JSON response."""
        return cls(status=status, body=json.dumps(payload, default=str).encode("utf-8"))

    @classmethod
    def text(cls, body: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> "Response":
        """Plain text response."""
        return cls(status=status, body=body.encode("utf-8"), content_type=content_type)

    @classmethod
    def empty(cls, status: int = 204) -> "Response":
        """Body-less response."""
        return cls(status=status, body=b"", content_type="")

    @classmethod
    def error(cls, status: int, message: str, hint: str = "") -> "Response":
        """Error response with a machine-readable and a human-readable part."""
        payload: Dict[str, Any] = {"error": message}
        if hint:
            payload["hint"] = hint
        return cls.json(payload, status=status)


Handler = Callable[[Request, Optional[Session]], Response]


@dataclass
class Route:
    method: str
    pattern: "re.Pattern"
    handler: Handler
    permission: Optional[Permission]
    public: bool = False


class Router:
    """Maps method and path to a handler plus the permission it requires."""

    def __init__(self) -> None:
        self._routes: List[Route] = []

    def add(
        self,
        method: str,
        path: str,
        handler: Handler,
        permission: Optional[Permission] = None,
        public: bool = False,
    ) -> None:
        """Register a route. `{name}` in the path becomes a named capture group."""
        pattern = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path) + "$"
        self._routes.append(Route(method.upper(), re.compile(pattern), handler, permission, public))

    def resolve(self, method: str, path: str) -> Tuple[Optional[Route], Dict[str, str], bool]:
        """Find a route. The third value is True when the path exists with another method."""
        path_exists = False
        for route in self._routes:
            match = route.pattern.match(path)
            if not match:
                continue
            path_exists = True
            if route.method == method.upper():
                return route, match.groupdict(), True
        return None, {}, path_exists


class RateLimiter:
    """Fixed-window counter that slows down password guessing per client address."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record an attempt and report whether it is within the limit."""
        now = time.monotonic()
        with self._lock:
            hits = [stamp for stamp in self._hits.get(key, []) if now - stamp < self._window]
            if len(hits) >= self._limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 2048:
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self._window}
            return True

    def reset(self, key: str) -> None:
        """Forget a client's attempts after a successful login."""
        with self._lock:
            self._hits.pop(key, None)


class ApiServer:
    """Owns the listeners and their lifecycle."""

    def __init__(self, context: AppContext) -> None:
        from .routes import build_internal_router, build_router

        self._context = context
        self._config = context.config
        self._limiter = RateLimiter(
            context.config.api.rate_limit.login_attempts, context.config.api.rate_limit.window_seconds
        )
        self._router = build_router(context, self._limiter)
        self._internal_router = build_internal_router(context)
        self._servers: List[ThreadingHTTPServer] = []
        self._threads: List[threading.Thread] = []

    def start(self) -> None:
        """Bind and serve both listeners."""
        self._start_listener(
            self._config.api.address,
            self._config.api.port,
            self._router,
            use_tls=self._config.api.tls.enabled,
            loopback_only=False,
            label="api",
        )
        self._start_listener(
            "127.0.0.1",
            self._config.api.internal_auth_port,
            self._internal_router,
            use_tls=False,
            loopback_only=True,
            label="internal",
        )

    def stop(self) -> None:
        """Shut both listeners down."""
        for server in self._servers:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        for thread in self._threads:
            thread.join(timeout=5)

    def _start_listener(
        self, address: str, port: int, router: Router, use_tls: bool, loopback_only: bool, label: str
    ) -> None:
        handler_class = _make_handler(router, self._context, loopback_only)
        try:
            server = _ReusableServer((address, port), handler_class)
        except OSError as exc:
            raise RuntimeError(
                f"Cannot listen on {address}:{port} for the {label} interface: {exc}\n"
                "  Why it probably failed: another process already uses the port, or the address does not\n"
                "  exist on this machine.\n"
                f"  Diagnose: sudo ss -tlnp | grep {port}\n"
                "  Fix: change api.port (or api.internal_auth_port) in /etc/securecam/config.yaml."
            ) from exc

        if use_tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                context.load_cert_chain(self._config.api.tls.cert_file, self._config.api.tls.key_file)
            except (OSError, ssl.SSLError) as exc:
                server.server_close()
                raise RuntimeError(
                    f"TLS is enabled but the certificate could not be loaded: {exc}\n"
                    "  Fix: check api.tls.cert_file and api.tls.key_file, or set api.tls.enabled to false."
                ) from exc
            server.socket = context.wrap_socket(server.socket, server_side=True)

        thread = threading.Thread(target=server.serve_forever, name=f"securecam-http-{label}", daemon=True)
        thread.start()
        self._servers.append(server)
        self._threads.append(thread)
        scheme = "https" if use_tls else "http"
        log.info("%s interface listening on %s://%s:%d", label.capitalize(), scheme, address, port)


class _ReusableServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


def _make_handler(router: Router, context: AppContext, loopback_only: bool):
    """Build a BaseHTTPRequestHandler subclass bound to one router."""

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "SecureCam"
        sys_version = ""

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_HEAD(self) -> None:
            self._dispatch("HEAD")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PATCH(self) -> None:
            self._dispatch("PATCH")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def do_OPTIONS(self) -> None:
            self._send(Response(status=204, content_type="", headers={"Allow": "GET, POST, PATCH, DELETE"}))

        def log_message(self, fmt: str, *args) -> None:
            log.debug("%s %s", self.address_string(), fmt % args)

        def _dispatch(self, method: str) -> None:
            client_ip = self.client_address[0] if self.client_address else ""
            if loopback_only and not _is_loopback(client_ip):
                log.warning("Rejected a non-loopback request to the internal interface from %s", client_ip)
                self._send(Response.error(403, "this interface only accepts local requests"))
                return
            try:
                request = self._build_request(method, client_ip)
            except HttpProblem as problem:
                self._send(Response.error(problem.status, problem.message, problem.hint))
                return

            route, params, path_exists = router.resolve(method if method != "HEAD" else "GET", request.path)
            if route is None:
                if path_exists:
                    self._send(Response.error(405, f"{method} is not allowed on {request.path}"))
                else:
                    self._send(Response.error(404, "not found"))
                return

            request.params = params
            session: Optional[Session] = None
            if not route.public:
                try:
                    session = _authenticate(request, context)
                except HttpProblem as problem:
                    self._send(Response.error(problem.status, problem.message, problem.hint))
                    return
                if route.permission is not None and not session.allows(route.permission):
                    log.warning(
                        "User '%s' (%s) was denied %s on %s", session.username, session.role, method, request.path
                    )
                    self._send(
                        Response.error(
                            403,
                            f"your role '{session.role}' is not allowed to do this",
                            "Ask an administrator of this camera to grant you the admin role.",
                        )
                    )
                    return
                if method in ("POST", "PATCH", "DELETE") and _used_cookie_auth(request):
                    if request.headers.get("x-requested-with", "") != "SecureCam":
                        self._send(Response.error(403, "missing the X-Requested-With header"))
                        return

            try:
                response = route.handler(request, session)
            except HttpProblem as problem:
                response = Response.error(problem.status, problem.message, problem.hint)
            except Exception:
                log.exception("Unhandled error while serving %s %s", method, request.path)
                response = Response.error(500, "internal error", "Check: journalctl -u securecam -n 50")
            self._send(response, head_only=(method == "HEAD"))

        def _build_request(self, method: str, client_ip: str) -> Request:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            if ".." in path:
                raise HttpProblem(400, "invalid path")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise HttpProblem(413, f"request body is larger than {MAX_BODY_BYTES} bytes")
            body = self.rfile.read(length) if length > 0 else b""
            headers = {key.lower(): value for key, value in self.headers.items()}
            return Request(
                method=method,
                path=path.rstrip("/") or "/",
                query=urllib.parse.parse_qs(parsed.query),
                headers=headers,
                body=body,
                client_ip=client_ip,
            )

        def _send(self, response: Response, head_only: bool = False) -> None:
            try:
                if response.file_path:
                    self._send_file(response, head_only)
                    return
                body = response.body
                self.send_response(response.status)
                if response.content_type:
                    self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.send_header("Content-Security-Policy", CSP)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                if body and not head_only:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_file(self, response: Response, head_only: bool) -> None:
            """Stream a file, honouring a single Range header so video seeking works."""
            path = response.file_path
            try:
                size = os.path.getsize(path)
            except OSError:
                self._send(Response.error(404, "file not found"))
                return

            start, end = 0, size - 1
            status = 200
            range_header = self.headers.get("Range", "")
            match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip()) if range_header else None
            if match and size:
                raw_start, raw_end = match.groups()
                if raw_start:
                    start = min(int(raw_start), size - 1)
                    end = int(raw_end) if raw_end else size - 1
                else:
                    start = max(0, size - int(raw_end or 0))
                end = min(end, size - 1)
                status = 206

            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", response.content_type or "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.send_header("Content-Security-Policy", CSP)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if head_only or not length:
                return
            try:
                with open(path, "rb") as handle:
                    handle.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = handle.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError as exc:
                log.warning("Could not stream %s: %s", path, exc)

    return _Handler


def _is_loopback(address: str) -> bool:
    """True for IPv4/IPv6 loopback addresses."""
    return address in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or address.startswith("127.")


def _used_cookie_auth(request: Request) -> bool:
    """True when the caller relied on the session cookie rather than a Bearer token."""
    return not request.headers.get("authorization", "").lower().startswith("bearer ")


def _authenticate(request: Request, context: AppContext) -> Session:
    """Resolve the caller's session from the Authorization header or the cookie."""
    from ..auth import TokenError

    token = ""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        token = _cookie_value(request.headers.get("cookie", ""), SESSION_COOKIE)
    if not token:
        raise HttpProblem(401, "authentication required", "Log in first: POST /api/login")

    try:
        payload = context.signer.verify(token, expected_kind="session")
    except TokenError as exc:
        raise HttpProblem(401, str(exc), "Log in again to get a fresh session token.") from exc

    if payload.get("did") != context.device_id:
        raise HttpProblem(403, "this session belongs to a different camera")

    user = context.users.get(str(payload.get("sub", "")))
    if user is None or not user.enabled:
        raise HttpProblem(401, "the account no longer exists or is disabled")
    role = user.role_for(context.device_id)
    if role is None:
        raise HttpProblem(403, "the account is no longer allowed to use this camera")

    return Session(
        user_id=user.id,
        username=user.username,
        role=role,
        device_id=context.device_id,
        expires_at=float(payload.get("exp", 0)),
    )


def _cookie_value(header: str, name: str) -> str:
    """Extract one cookie value without pulling in http.cookies."""
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.strip()
    return ""


def static_file(relative: str) -> Optional[str]:
    """Resolve a path inside the packaged static directory, or None if it escapes."""
    candidate = os.path.normpath(os.path.join(STATIC_DIR, relative.lstrip("/")))
    if not candidate.startswith(STATIC_DIR) or not os.path.isfile(candidate):
        return None
    return candidate


def guess_type(path: str) -> str:
    """Content types for the handful of files the UI ships."""
    mapping = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".json": "application/json",
    }
    return mapping.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def local_addresses(port: int) -> List[str]:
    """Best-effort URLs this API is reachable on, for the installer summary."""
    urls = []
    try:
        hostname = socket.gethostname()
        urls.append(f"http://{hostname}.local:{port}")
    except OSError:
        pass
    return urls
