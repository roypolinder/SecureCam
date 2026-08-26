import pytest

from securecam.ai import AIError, create_provider
from securecam.ai.base import coerce_bool, coerce_confidence, parse_json_answer
from securecam.config import parse_config


def test_disabled_provider_is_used_when_ai_is_off(config):
    provider = create_provider(config.ai)
    assert provider.enabled is False


def test_openai_provider_requires_a_key(raw_config):
    raw_config["ai"] = {"enabled": True, "provider": "openai_vision", "openai_vision": {"api_key_env": "NOT_SET_HERE"}}
    config = parse_config(raw_config, "test.yaml")
    ok, message = create_provider(config.ai).check()
    assert ok is False
    assert "NOT_SET_HERE" in message


def test_openai_provider_accepts_a_key(raw_config, monkeypatch):
    monkeypatch.setenv("TEST_AI_KEY", "sk-test")
    raw_config["ai"] = {"enabled": True, "provider": "openai_vision", "openai_vision": {"api_key_env": "TEST_AI_KEY"}}
    config = parse_config(raw_config, "test.yaml")
    ok, message = create_provider(config.ai).check()
    assert ok is True, message


def test_generic_provider_requires_an_endpoint(raw_config):
    from securecam.config import ConfigError

    raw_config["ai"] = {"enabled": True, "provider": "generic_http", "generic_http": {"endpoint": ""}}
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw_config, "test.yaml")
    assert "generic_http" in excinfo.value.render()


def test_generic_provider_accepts_an_endpoint(raw_config):
    raw_config["ai"] = {
        "enabled": True,
        "provider": "generic_http",
        "generic_http": {"endpoint": "https://example.invalid/detect"},
    }
    config = parse_config(raw_config, "test.yaml")
    ok, message = create_provider(config.ai).check()
    assert ok is True, message


def test_parse_plain_json():
    assert parse_json_answer('{"person_detected": true}') == {"person_detected": True}


def test_parse_json_in_a_code_fence():
    reply = "Sure!\n```json\n{\"person_detected\": false}\n```\n"
    assert parse_json_answer(reply) == {"person_detected": False}


def test_parse_json_embedded_in_prose():
    reply = 'I think {"person_detected": true, "confidence": 0.8} describes it.'
    assert parse_json_answer(reply)["confidence"] == 0.8


def test_non_json_reply_is_not_retryable():
    with pytest.raises(AIError) as excinfo:
        parse_json_answer("there is a person")
    assert excinfo.value.retryable is False


@pytest.mark.parametrize(
    "value,expected",
    [(True, True), ("yes", True), ("TRUE", True), ("no", False), ("0", False), (1, True), ("maybe", None), (None, None)],
)
def test_coerce_bool(value, expected):
    assert coerce_bool(value) is expected


@pytest.mark.parametrize(
    "value,expected", [(0.8, 0.8), ("0.8", 0.8), (80, 0.8), (150, 1.0), (-1, 0.0), ("high", None), (None, None)]
)
def test_coerce_confidence(value, expected):
    assert coerce_confidence(value) == expected
