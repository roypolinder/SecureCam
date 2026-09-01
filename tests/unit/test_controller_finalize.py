from types import SimpleNamespace

import pytest

from securecam.events import EventStore
from securecam.main import Controller
from securecam.statemachine import FinalizeReason, MotionStateMachine
from securecam.util import utcnow


@pytest.fixture()
def controller(config, events_dir):
    """A controller with only the pieces _finalize_event touches, so no /etc access is needed."""
    instance = Controller.__new__(Controller)
    instance.config = config
    instance.device_id = "test-cam"
    instance.store = EventStore(events_dir)
    instance.machine = MotionStateMachine(post_motion_seconds=300, max_event_seconds=1800)
    instance.tasks = SimpleNamespace(submit=lambda *args: None)
    instance.pipeline = SimpleNamespace(on_event_finalized=lambda event: None)
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
