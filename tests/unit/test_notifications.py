import pytest

from securecam.config import ConfigError, parse_config
from securecam.notifications import create_provider
from securecam.notifications.base import Notification, ascii_header


def with_notifications(raw_config, **overrides):
    raw_config["notifications"] = {"enabled": True, "provider": "ntfy", **overrides}
    return raw_config


def test_disabled_when_notifications_are_off(config):
    assert create_provider(config.notifications).enabled is False


def test_enabling_without_a_recipient_is_refused(raw_config):
    with pytest.raises(ConfigError) as excinfo:
        parse_config(with_notifications(raw_config), "test.yaml")
    assert "recipient" in excinfo.value.render().lower()


def test_placeholder_targets_are_refused(raw_config):
    raw_config = with_notifications(raw_config, recipients=[{"id": "phone", "enabled": True, "target": "CHANGE-ME"}])
    with pytest.raises(ConfigError):
        parse_config(raw_config, "test.yaml")


def test_a_real_ntfy_topic_is_accepted(raw_config):
    raw_config = with_notifications(
        raw_config, recipients=[{"id": "phone", "enabled": True, "target": "my-secret-topic"}]
    )
    config = parse_config(raw_config, "test.yaml")
    ok, message = create_provider(config.notifications).check()
    assert ok is True, message


def test_active_recipients_only_includes_enabled_ones(raw_config):
    raw_config = with_notifications(
        raw_config,
        recipients=[
            {"id": "phone", "enabled": True, "target": "topic-one"},
            {"id": "tablet", "enabled": False, "target": "topic-two"},
        ],
    )
    config = parse_config(raw_config, "test.yaml")
    assert [r.id for r in config.notifications.active_recipients()] == ["phone"]


def test_provider_dispatch(raw_config):
    raw_config = with_notifications(raw_config, recipients=[{"id": "phone", "enabled": True, "target": "topic-one"}])
    config = parse_config(raw_config, "test.yaml")
    assert create_provider(config.notifications).name == "ntfy"
    assert create_provider(config.notifications, "disabled").enabled is False


def test_ascii_header_strips_unsupported_characters():
    assert "\n" not in ascii_header("line one\nline two")
    assert ascii_header("caf\u00e9").isascii()


def test_notification_carries_the_alarm_flag():
    assert Notification(title="t", message="m", alarm=True).alarm is True
