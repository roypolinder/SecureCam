"""API endpoints. Every route declares the permission it needs."""

from __future__ import annotations

import os
import urllib.parse
from typing import Any, Dict, Optional

from ..appcontext import AppContext
from ..auth import Permission
from ..auth.store import AuthError
from ..auth.tokens import TokenError
from ..config import redacted_dict
from ..diagnostics import build_report
from ..logging_setup import get_logger
from .server import (
    SESSION_COOKIE,
    HttpProblem,
    RateLimiter,
    Request,
    Response,
    Router,
    Session,
    guess_type,
    static_file,
)

log = get_logger("api.routes")


def build_router(context: AppContext, limiter: RateLimiter) -> Router:
    """Public API and, when enabled, the web UI."""
    router = Router()
    config = context.config

    # -- unauthenticated ----------------------------------------------------

    def info(request: Request, session: Optional[Session]) -> Response:
        """Minimal facts the login page needs before anyone is authenticated."""
        return Response.json(
            {
                "name": config.device.name,
                "device_id": context.device_id,
                "version": context.version,
                "web_ui": config.api.web_ui,
            }
        )

    def login(request: Request, session: Optional[Session]) -> Response:
        """Exchange a username and password for a session token."""
        if not limiter.allow(request.client_ip or "unknown"):
            log.warning("Rate limited login attempts from %s", request.client_ip)
            raise HttpProblem(
                429,
                "too many login attempts",
                f"Wait {config.api.rate_limit.window_seconds} seconds and try again.",
            )
        payload = request.json()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        if not username or not password:
            raise HttpProblem(400, "username and password are required")

        user = context.users.authenticate(username, password, context.device_id)
        if user is None:
            log.warning("Failed login for '%s' from %s", username, request.client_ip)
            raise HttpProblem(401, "invalid username or password")

        limiter.reset(request.client_ip or "unknown")
        role = user.role_for(context.device_id) or "viewer"
        ttl = config.api.session_ttl_hours * 3600
        token = context.signer.issue(
            {"kind": "session", "sub": user.username, "uid": user.id, "did": context.device_id, "role": role}, ttl
        )
        log.info("User '%s' logged in from %s as %s", user.username, request.client_ip, role)
        secure = "; Secure" if config.api.tls.enabled else ""
        response = Response.json(
            {
                "token": token,
                "expires_in": int(ttl),
                "user": {"username": user.username, "role": role, "device_id": context.device_id},
            }
        )
        response.headers["Set-Cookie"] = (
            f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={int(ttl)}{secure}"
        )
        return response

    router.add("GET", "/api/info", info, public=True)
    router.add("POST", "/api/login", login, public=True)

    # -- session ------------------------------------------------------------

    def logout(request: Request, session: Optional[Session]) -> Response:
        """Clear the browser cookie. Bearer tokens simply expire."""
        return Response(
            status=200,
            body=b'{"ok":true}',
            headers={"Set-Cookie": f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"},
        )

    def whoami(request: Request, session: Optional[Session]) -> Response:
        """Details about the current session."""
        assert session is not None
        return Response.json(
            {
                "username": session.username,
                "role": session.role,
                "device_id": session.device_id,
                "expires_at": session.expires_at,
                "permissions": sorted(p.value for p in Permission if session.allows(p)),
            }
        )

    router.add("POST", "/api/logout", logout)
    router.add("GET", "/api/session", whoami)

    # -- status -------------------------------------------------------------

    def health(request: Request, session: Optional[Session]) -> Response:
        """Aggregated subsystem health."""
        if context.health is None:
            raise HttpProblem(503, "health monitoring has not started yet")
        return Response.json(context.health.report())

    def diagnostics(request: Request, session: Optional[Session]) -> Response:
        """Full diagnostic report, the same data as `securecam-admin diagnose --json`."""
        return Response.json(build_report(context))

    router.add("GET", "/api/health", health, Permission.VIEW_EVENTS)
    router.add("GET", "/api/diagnostics", diagnostics, Permission.MANAGE_DEVICE)

    # -- arming -------------------------------------------------------------

    def arming(request: Request, session: Optional[Session]) -> Response:
        """Whether motion currently creates recordings."""
        if context.arming is None:
            raise HttpProblem(503, "the controller has not started yet")
        return Response.json(context.arming.status())

    def set_arming(request: Request, session: Optional[Session]) -> Response:
        """Arm or disarm motion recording. Live viewing is unaffected either way."""
        if context.set_armed is None:
            raise HttpProblem(503, "the controller has not started yet")
        payload = request.json()
        if not isinstance(payload.get("armed"), bool):
            raise HttpProblem(400, "'armed' must be true or false")
        assert session is not None
        return Response.json(context.set_armed(payload["armed"], session.username))

    router.add("GET", "/api/arming", arming, Permission.VIEW_EVENTS)
    router.add("POST", "/api/arming", set_arming, Permission.MANAGE_DEVICE)

    # -- events -------------------------------------------------------------

    def list_events(request: Request, session: Optional[Session]) -> Response:
        """Newest-first page of events."""
        limit = request.int_param("limit", 50, 1, 200)
        offset = request.int_param("offset", 0, 0, 100_000)
        status = request.param("status")
        person_only = request.param("person") == "true"
        events = context.store.list(limit=limit, offset=offset, status=status, person_only=person_only)
        return Response.json({"events": [event.summary() for event in events], "limit": limit, "offset": offset})

    def get_event(request: Request, session: Optional[Session]) -> Response:
        """Full metadata for a single event."""
        event = _require_event(context, request.params["event_id"])
        payload = event.to_dict()
        payload["event_id"] = event.event_id
        return Response.json(payload)

    def event_snapshot(request: Request, session: Optional[Session]) -> Response:
        """The event's JPEG snapshot."""
        event = _require_event(context, request.params["event_id"])
        name = os.path.basename(request.param("file", "snapshot.jpg"))
        if name not in event.snapshot.paths and name != "snapshot.jpg":
            raise HttpProblem(404, "that snapshot does not belong to this event")
        path = os.path.join(event.directory, name)
        if not os.path.isfile(path):
            raise HttpProblem(404, "no snapshot was saved for this event")
        return Response(status=200, content_type="image/jpeg", file_path=path, headers={"Cache-Control": "private, max-age=3600"})

    def event_video(request: Request, session: Optional[Session]) -> Response:
        """The event's MP4 clip, with byte-range support so browsers can seek."""
        event = _require_event(context, request.params["event_id"])
        path = event.recording_path
        if not os.path.isfile(path):
            raise HttpProblem(
                404,
                f"no video is stored for this event (recording state: {event.recording.state})",
                event.recording.last_error or "The rolling buffer may have expired before extraction finished.",
            )
        return Response(status=200, content_type="video/mp4", file_path=path)

    def delete_event(request: Request, session: Optional[Session]) -> Response:
        """Delete one event and everything in it."""
        event_id = request.params["event_id"]
        try:
            deleted = context.store.delete(event_id)
        except ValueError as exc:
            raise HttpProblem(409, str(exc)) from exc
        if not deleted:
            raise HttpProblem(404, "event not found")
        assert session is not None
        log.info("User '%s' deleted event %s", session.username, event_id)
        return Response.json({"ok": True})

    router.add("GET", "/api/events", list_events, Permission.VIEW_EVENTS)
    router.add("GET", "/api/events/{event_id}", get_event, Permission.VIEW_EVENTS)
    router.add("GET", "/api/events/{event_id}/snapshot", event_snapshot, Permission.VIEW_EVENTS)
    router.add("GET", "/api/events/{event_id}/video", event_video, Permission.VIEW_EVENTS)
    router.add("DELETE", "/api/events/{event_id}", delete_event, Permission.MANAGE_DEVICE)

    # -- live stream --------------------------------------------------------

    def stream_ticket(request: Request, session: Optional[Session]) -> Response:
        """Issue short-lived credentials the browser uses for the WebRTC handshake."""
        assert session is not None
        ttl = config.api.stream_ticket_ttl_seconds
        token = context.signer.issue(
            {
                "kind": "stream",
                "sub": session.username,
                "did": context.device_id,
                "path": config.mediamtx.path_name,
                "role": session.role,
            },
            ttl,
        )
        host = _request_host(request)
        base = f"http://{host}:{config.mediamtx.webrtc_port}/{config.mediamtx.path_name}"
        return Response.json(
            {
                "username": "ticket",
                "password": token,
                "expires_in": ttl,
                "whep_url": f"{base}/whep",
                "path": config.mediamtx.path_name,
            }
        )

    router.add("POST", "/api/stream/ticket", stream_ticket, Permission.VIEW_LIVE)

    # -- configuration and users -------------------------------------------

    def get_config(request: Request, session: Optional[Session]) -> Response:
        """Current configuration with identifiers masked."""
        return Response.json(redacted_dict(config))

    def list_users(request: Request, session: Optional[Session]) -> Response:
        """All accounts known to this camera."""
        return Response.json({"users": [user.public(context.device_id) for user in context.users.list_users()]})

    def create_user(request: Request, session: Optional[Session]) -> Response:
        """Create an account bound to this camera."""
        payload = request.json()
        try:
            user = context.users.add_user(
                str(payload.get("username", "")),
                str(payload.get("password", "")),
                str(payload.get("role", "viewer")),
            )
        except AuthError as exc:
            raise HttpProblem(400, str(exc)) from exc
        assert session is not None
        log.info("User '%s' created account '%s'", session.username, user.username)
        return Response.json(user.public(context.device_id), status=201)

    def update_user(request: Request, session: Optional[Session]) -> Response:
        """Change an account's role, password or enabled flag."""
        username = request.params["username"]
        payload = request.json()
        try:
            if "password" in payload:
                context.users.set_password(username, str(payload["password"]))
            if "role" in payload:
                context.users.set_role(username, str(payload["role"]))
            if "enabled" in payload:
                context.users.set_enabled(username, bool(payload["enabled"]))
        except AuthError as exc:
            raise HttpProblem(400, str(exc)) from exc
        user = context.users.get(username)
        if user is None:
            raise HttpProblem(404, "user not found")
        return Response.json(user.public(context.device_id))

    def delete_user(request: Request, session: Optional[Session]) -> Response:
        """Delete an account."""
        try:
            context.users.delete_user(request.params["username"])
        except AuthError as exc:
            raise HttpProblem(400, str(exc)) from exc
        return Response.json({"ok": True})

    router.add("GET", "/api/config", get_config, Permission.CONFIGURE)
    router.add("GET", "/api/users", list_users, Permission.MANAGE_USERS)
    router.add("POST", "/api/users", create_user, Permission.MANAGE_USERS)
    router.add("PATCH", "/api/users/{username}", update_user, Permission.MANAGE_USERS)
    router.add("DELETE", "/api/users/{username}", delete_user, Permission.MANAGE_USERS)

    # -- web UI -------------------------------------------------------------

    if config.api.web_ui:
        def index(request: Request, session: Optional[Session]) -> Response:
            """Serve the single-page UI."""
            path = static_file("index.html")
            if path is None:
                raise HttpProblem(500, "the web UI files are missing from the installation")
            return Response(status=200, content_type="text/html; charset=utf-8", file_path=path)

        def asset(request: Request, session: Optional[Session]) -> Response:
            """Serve a static asset."""
            path = static_file(request.params["name"])
            if path is None:
                raise HttpProblem(404, "not found")
            return Response(status=200, content_type=guess_type(path), file_path=path)

        router.add("GET", "/", index, public=True)
        router.add("GET", "/static/{name}", asset, public=True)

    return router


def build_internal_router(context: AppContext) -> Router:
    """Loopback-only router that answers MediaMTX authentication callbacks."""
    router = Router()
    config = context.config
    service_user, service_password = context.service_credentials

    def mediamtx_auth(request: Request, session: Optional[Session]) -> Response:
        """Authorize one MediaMTX action. Any non-2xx status denies it."""
        payload = request.json()
        action = str(payload.get("action", ""))
        user = str(payload.get("user", ""))
        password = str(payload.get("password", ""))
        path = str(payload.get("path", ""))
        source_ip = str(payload.get("ip", ""))

        if action in ("api", "metrics", "pprof"):
            if _valid_service(user, password, service_user, service_password) and _loopback(source_ip):
                return Response.empty(200)
            return _deny(action, user, source_ip, "control API access is limited to the local service account")

        if action == "publish":
            return _deny(action, user, source_ip, "publishing is not allowed; the camera is the only source")

        if action not in ("read", "playback"):
            return _deny(action, user, source_ip, "unknown action")

        if path and path != config.mediamtx.path_name:
            return _deny(action, user, source_ip, f"path '{path}' does not exist")

        if _valid_service(user, password, service_user, service_password) and _loopback(source_ip):
            return Response.empty(200)

        if user == "ticket":
            try:
                claims = context.signer.verify(password, expected_kind="stream")
            except TokenError as exc:
                return _deny(action, user, source_ip, f"stream ticket rejected: {exc}")
            if claims.get("did") != context.device_id:
                return _deny(action, user, source_ip, "stream ticket belongs to another camera")
            if claims.get("path") and claims["path"] != config.mediamtx.path_name:
                return _deny(action, user, source_ip, "stream ticket is for another path")
            return Response.empty(200)

        account = context.users.authenticate(user, password, context.device_id)
        if account is None:
            return _deny(action, user, source_ip, "invalid credentials")
        role = account.role_for(context.device_id) or "viewer"
        from ..auth import has_permission

        needed = Permission.VIEW_LIVE if action == "read" else Permission.VIEW_EVENTS
        if not has_permission(role, needed):
            return _deny(action, user, source_ip, f"role '{role}' may not {action}")
        return Response.empty(200)

    router.add("POST", "/internal/mediamtx/auth", mediamtx_auth, public=True)
    return router


def _valid_service(user: str, password: str, expected_user: str, expected_password: str) -> bool:
    """Constant-time comparison of the internal service credentials."""
    import hmac

    return bool(expected_password) and hmac.compare_digest(user, expected_user) and hmac.compare_digest(
        password, expected_password
    )


def _loopback(address: str) -> bool:
    """True for loopback source addresses reported by MediaMTX."""
    host = address.rsplit(":", 1)[0] if address.count(":") == 1 else address
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


def _deny(action: str, user: str, source_ip: str, reason: str) -> Response:
    """Log and refuse a MediaMTX request."""
    log.warning("Denied MediaMTX %s for user '%s' from %s: %s", action or "?", user or "anonymous", source_ip, reason)
    return Response.error(401, reason)


def _require_event(context: AppContext, event_id: str):
    """Load an event or raise a 404."""
    event = context.store.get(event_id)
    if event is None:
        raise HttpProblem(404, "event not found")
    return event


def _request_host(request: Request) -> str:
    """Hostname the client used, so WebRTC URLs work on LAN and over Tailscale alike."""
    host = request.headers.get("host", "")
    if not host:
        return "127.0.0.1"
    parsed = urllib.parse.urlsplit(f"//{host}")
    return parsed.hostname or "127.0.0.1"
