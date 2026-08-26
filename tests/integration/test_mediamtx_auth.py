import json
from types import SimpleNamespace

import pytest

from securecam.api.routes import build_internal_router
from securecam.api.server import Request
from securecam.auth import TokenSigner, UserStore

SERVICE_USER = "securecam-internal"
SERVICE_PASSWORD = "service-password-value"
ADMIN_PASSWORD = "correct-horse-battery"


@pytest.fixture
def context(config, tmp_path):
    users = UserStore(str(tmp_path / "users.json"), config.device.id)
    users.add_user("alice", ADMIN_PASSWORD, "admin")
    return SimpleNamespace(
        config=config,
        device_id=config.device.id,
        users=users,
        signer=TokenSigner(b"integration-test-secret"),
        service_credentials=(SERVICE_USER, SERVICE_PASSWORD),
    )


def ask(context, **payload):
    router = build_internal_router(context)
    route, params, _ = router.resolve("POST", "/internal/mediamtx/auth")
    assert route is not None
    request = Request(
        method="POST",
        path="/internal/mediamtx/auth",
        query={},
        headers={},
        body=json.dumps(payload).encode("utf-8"),
        client_ip="127.0.0.1",
        params=params,
    )
    return route.handler(request, None)


def test_the_route_is_public_so_mediamtx_can_reach_it(context):
    route, _, _ = build_internal_router(context).resolve("POST", "/internal/mediamtx/auth")
    assert route.public is True


def test_service_account_may_read(context):
    response = ask(context, action="read", user=SERVICE_USER, password=SERVICE_PASSWORD, path="cam", ip="127.0.0.1")
    assert response.status == 200


def test_service_account_from_the_network_is_denied(context):
    response = ask(context, action="api", user=SERVICE_USER, password=SERVICE_PASSWORD, ip="192.168.1.50")
    assert response.status == 401


def test_publishing_is_always_denied(context):
    response = ask(context, action="publish", user=SERVICE_USER, password=SERVICE_PASSWORD, ip="127.0.0.1")
    assert response.status == 401
    assert "publishing" in json.loads(response.body)["error"]


def test_unknown_action_is_denied(context):
    assert ask(context, action="teleport", user="alice", password=ADMIN_PASSWORD, ip="10.0.0.5").status == 401


def test_unknown_path_is_denied(context):
    response = ask(context, action="read", user="alice", password=ADMIN_PASSWORD, path="secret", ip="10.0.0.5")
    assert response.status == 401


def test_a_valid_stream_ticket_is_accepted(context):
    token = context.signer.issue({"kind": "stream", "did": context.device_id, "path": "cam"}, 30)
    assert ask(context, action="read", user="ticket", password=token, path="cam", ip="10.0.0.5").status == 200


def test_an_expired_stream_ticket_is_rejected(context):
    token = context.signer.issue({"kind": "stream", "did": context.device_id, "path": "cam"}, -1)
    assert ask(context, action="read", user="ticket", password=token, path="cam", ip="10.0.0.5").status == 401


def test_a_ticket_for_another_camera_is_rejected(context):
    token = context.signer.issue({"kind": "stream", "did": "some-other-camera", "path": "cam"}, 30)
    assert ask(context, action="read", user="ticket", password=token, path="cam", ip="10.0.0.5").status == 401


def test_a_session_token_cannot_be_used_as_a_stream_ticket(context):
    token = context.signer.issue({"kind": "session", "sub": "alice", "did": context.device_id}, 30)
    assert ask(context, action="read", user="ticket", password=token, path="cam", ip="10.0.0.5").status == 401


def test_a_real_user_may_read(context):
    assert ask(context, action="read", user="alice", password=ADMIN_PASSWORD, path="cam", ip="10.0.0.5").status == 200


def test_a_wrong_password_is_denied(context):
    response = ask(context, action="read", user="alice", password="nope-nope-nope", path="cam", ip="10.0.0.5")
    assert response.status == 401


def test_anonymous_access_is_denied(context):
    assert ask(context, action="read", user="", password="", path="cam", ip="10.0.0.5").status == 401


def test_denials_do_not_leak_the_service_password(context):
    response = ask(context, action="read", user="alice", password=SERVICE_PASSWORD, path="cam", ip="10.0.0.5")
    assert SERVICE_PASSWORD not in response.body.decode("utf-8")
