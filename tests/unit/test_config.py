import pytest

from securecam.config import ConfigError, parse_config


def test_defaults_are_valid(config):
    assert config.device.id == "test-cam"
    assert config.camera.width == 1920
    assert config.mediamtx.path_name == "cam"


def test_unknown_keys_are_collected_not_fatal(raw_config):
    raw_config["camera"]["widht"] = 1280
    config = parse_config(raw_config, "test.yaml")
    assert any("widht" in key for key in config.unknown_keys)


def test_strict_unknown_raises(raw_config):
    raw_config["camera"]["widht"] = 1280
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw_config, "test.yaml", strict_unknown=True)
    assert "widht" in excinfo.value.render()


def test_out_of_range_value_is_reported(raw_config):
    raw_config["camera"]["fps"] = 0
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw_config, "test.yaml")
    assert "fps" in excinfo.value.render()


def test_bad_choice_is_reported(raw_config):
    raw_config["motion"]["source"] = "radar"
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw_config, "test.yaml")
    assert "radar" in excinfo.value.render()


def test_buffer_must_outlive_the_longest_event(raw_config):
    raw_config["storage"]["buffer_retain_minutes"] = 2
    raw_config["motion"] = {"pre_event_seconds": 60, "max_event_seconds": 1800}
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw_config, "test.yaml")
    assert "buffer" in excinfo.value.render().lower()


def test_port_collision_is_reported(raw_config):
    raw_config["mediamtx"]["rtsp_address"] = "127.0.0.1:9997"
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw_config, "test.yaml")
    assert "9997" in excinfo.value.render()


def test_odd_dimensions_are_reported(raw_config):
    raw_config["camera"]["width"] = 1921
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw_config, "test.yaml")
    assert "even" in excinfo.value.render().lower()


def test_config_error_lists_every_problem(raw_config):
    raw_config["camera"]["fps"] = 0
    raw_config["motion"]["source"] = "radar"
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw_config, "test.yaml")
    assert len(excinfo.value.problems) >= 2


def test_all_errors_mention_the_file(raw_config):
    raw_config["camera"]["fps"] = 0
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw_config, "/etc/securecam/config.yaml")
    assert "/etc/securecam/config.yaml" in excinfo.value.render()
