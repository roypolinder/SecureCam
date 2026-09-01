import os

import pytest

from securecam.arming import ArmingState


@pytest.fixture()
def state_path(tmp_path):
    return str(tmp_path / "arm-state.json")


def test_defaults_to_the_configured_value(state_path):
    assert ArmingState(state_path, default_armed=True).armed is True
    assert ArmingState(str(state_path) + ".other", default_armed=False).armed is False


def test_setting_the_same_value_is_not_a_change(state_path):
    arming = ArmingState(state_path, default_armed=True)
    assert arming.set(True, "alice") is False
    assert arming.set(False, "alice") is True


def test_the_state_survives_a_restart(state_path):
    ArmingState(state_path, default_armed=True).set(False, "alice")
    assert os.path.exists(state_path)
    reloaded = ArmingState(state_path, default_armed=True)
    assert reloaded.armed is False
    assert reloaded.status()["changed_by"] == "alice"


def test_a_corrupt_state_file_falls_back_to_the_default(state_path):
    with open(state_path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert ArmingState(state_path, default_armed=True).armed is True


def test_status_reports_who_changed_it(state_path):
    arming = ArmingState(state_path, default_armed=True)
    arming.set(False, "bob")
    status = arming.status()
    assert status["armed"] is False
    assert status["changed_by"] == "bob"
    assert status["changed_at"]
