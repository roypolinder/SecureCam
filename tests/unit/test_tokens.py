import time

import pytest

from securecam.auth.tokens import TokenError, TokenSigner


def test_round_trip():
    signer = TokenSigner(b"unit-test-secret")
    token = signer.issue({"kind": "session", "sub": "alice"}, 60)
    payload = signer.verify(token, expected_kind="session")
    assert payload["sub"] == "alice"


def test_expired_token_is_rejected():
    signer = TokenSigner(b"unit-test-secret")
    token = signer.issue({"kind": "session"}, -1)
    time.sleep(0.01)
    with pytest.raises(TokenError):
        signer.verify(token)


def test_tampered_payload_is_rejected():
    signer = TokenSigner(b"unit-test-secret")
    token = signer.issue({"kind": "session", "role": "viewer"}, 60)
    body, _, signature = token.partition(".")
    with pytest.raises(TokenError):
        signer.verify(body[:-2] + "AA." + signature)


def test_another_key_cannot_verify():
    token = TokenSigner(b"key-one").issue({"kind": "session"}, 60)
    with pytest.raises(TokenError):
        TokenSigner(b"key-two").verify(token)


def test_wrong_kind_is_rejected():
    signer = TokenSigner(b"unit-test-secret")
    token = signer.issue({"kind": "stream"}, 60)
    with pytest.raises(TokenError):
        signer.verify(token, expected_kind="session")


def test_malformed_tokens_are_rejected():
    signer = TokenSigner(b"unit-test-secret")
    for bad in ("", "nodot", "a.b.c"):
        with pytest.raises(TokenError):
            signer.verify(bad)


def test_empty_secret_is_refused():
    with pytest.raises(ValueError):
        TokenSigner(b"")
