from securecam.mediamtx import render_config, write_config


def test_render_contains_the_camera_source(config):
    text = render_config(config, "svc", "secret")
    assert "source: rpiCamera" in text
    assert f"  {config.mediamtx.path_name}:" in text


def test_render_uses_the_configured_resolution(raw_config):
    from securecam.config import parse_config

    raw_config["camera"].update({"width": 1280, "height": 720, "fps": 20})
    config = parse_config(raw_config, "test.yaml")
    text = render_config(config, "svc", "secret")
    assert "rpiCameraWidth: 1280" in text
    assert "rpiCameraHeight: 720" in text
    assert "rpiCameraFPS: 20" in text


def test_recording_is_enabled_with_a_bounded_window(config):
    text = render_config(config, "svc", "secret")
    assert "record: yes" in text
    assert "recordFormat: fmp4" in text
    assert f"recordDeleteAfter: {config.storage.buffer_retain_minutes}m" in text
    assert config.storage.buffer_path in text


def test_http_auth_points_at_the_internal_listener(raw_config):
    from securecam.config import parse_config

    raw_config["api"] = {"enabled": True, "internal_auth_port": 9095}
    config = parse_config(raw_config, "test.yaml")
    text = render_config(config, "svc", "secret")
    assert "authMethod: http" in text
    assert "127.0.0.1:9095/internal/mediamtx/auth" in text


def test_internal_auth_falls_back_to_a_local_account(config):
    text = render_config(config, "svc", "s3cret")
    assert "authMethod: internal" in text
    assert "svc" in text


def test_secrets_are_not_written_when_http_auth_is_used(raw_config):
    from securecam.config import parse_config

    raw_config["api"] = {"enabled": True}
    config = parse_config(raw_config, "test.yaml")
    assert "hunter2hunter2" not in render_config(config, "svc", "hunter2hunter2")


def test_write_config_is_idempotent(config):
    assert write_config(config, "svc", "secret") is True
    assert write_config(config, "svc", "secret") is False


def test_write_config_reports_a_change(config):
    write_config(config, "svc", "secret")
    config.camera.fps = 25
    assert write_config(config, "svc", "secret") is True
