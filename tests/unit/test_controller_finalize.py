import threading
from types import SimpleNamespace

import pytest

from securecam.arming import ArmingState
from securecam.events import EventStore
from securecam.main import Controller
from securecam.statemachine import EventState, FinalizeReason, MotionStateMachine
from securecam.util import utcnow


@pytest.fixture()
def controller(config, events_dir, tmp_path):
    """A controller with only the pieces _finalize_event touches, so no /etc access is needed."""
    instance = Controller.__new__(Controller)
    instance.config = config
    instance.device_id = "test-cam"
    instance._lock = threading.RLock()
    instance.store = EventStore(events_dir)
    instance.machine = MotionStateMachine(post_motion_seconds=300, max_event_seconds=1800)
    instance.arming = ArmingState(str(tmp_path / "arm-state.json"), True)
    instance.pir = SimpleNamespace(motion_active=True)
    instance.health = SimpleNamespace(collect=lambda: None)
    instance.tasks = SimpleNamespace(submit=lambda *args: None)
    instance.pipeline = SimpleNamespace(
        on_event_started=lambda event: None, on_event_finalized=lambda event: None
    )
    instance._event_start_wall = utcnow()
    instance._event_start_mono = 0.0
    instance._current = instance.store.create("test-cam", config.device.name, instance._event_start_wall, 60)
    instance.machine.on_motion_start(0.0)
    instance.machine.mark_recording()
    return instance


def test_finalizing_closes_the_open_motion_segment(controller):
    event = controller._current
    controller._finalize_event(30.0, FinalizeReason.DISARMED)
    assert controller._current is None
    assert event.motion_segments[-1]["end"] is not None
    assert event.finalize_reason == "disarmed"
    assert event.ended_at


def test_a_finalized_event_leaves_no_open_segment_on_disk(controller):
    event_id = controller._current.event_id
    controller._finalize_event(30.0, FinalizeReason.DISARMED)
    stored = controller.store.get(event_id)
    assert all(segment["end"] for segment in stored.motion_segments)
    assert stored.status != "recording"


def test_disarming_stops_now_and_held_motion_does_not_start_a_replacement(controller):
    event_id = controller._current.event_id
    controller.set_armed(False, "alice")

    assert controller._current is None
    stored = controller.store.get(event_id)
    assert stored.finalize_reason == "disarmed"
    assert stored.ended_at

    # The PIR is still reading motion; the control loop must not open a second event.
    for now in (31.0, 120.0, 700.0, 2000.0):
        with controller._lock:
            if not controller.arming.armed and controller.machine.motion_active:
                controller._apply(controller.machine.suspend(now, FinalizeReason.DISARMED), now)
            controller._apply(controller.machine.tick(now), now)
    assert controller._current is None
    assert len(list(controller.store.iter_events())) == 1


def test_arming_again_picks_up_motion_that_is_already_present(controller):
    controller.set_armed(False, "alice")
    assert controller._current is None
    controller.set_armed(True, "alice")
    assert controller._current is not None
    assert controller.machine.state is EventState.RECORDING


def test_arming_again_stays_idle_when_the_sensor_is_quiet(controller):
    controller.set_armed(False, "alice")
    controller.pir.motion_active = False
    controller.set_armed(True, "alice")
    assert controller._current is None
    assert controller.machine.state is EventState.IDLE
