import pytest

from securecam.ai.base import AIProvider, AIResult
from securecam.events import EventStore, TaskState
from securecam.mediamtx import MediaMTXClient
from securecam.pipeline import EventPipeline
from securecam.util import utcnow


class FakeProvider(AIProvider):
    """Returns a scripted verdict per call and counts how often it was asked."""

    name = "fake"

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return True

    def check(self):
        return True, "ok"

    def analyze(self, images, context):
        self.calls += 1
        person = self.verdicts[min(self.calls - 1, len(self.verdicts) - 1)]
        return AIResult(person_detected=person, confidence=0.9, label="person" if person else "empty", summary="")


class FakeSnapshotter:
    available = True

    def __init__(self, directory_writes=True):
        self.captures = 0
        self.directory_writes = directory_writes

    def capture(self, destination, timeout: float = 15.0) -> int:
        self.captures += 1
        with open(destination, "wb") as handle:
            handle.write(b"jpeg-bytes")
        return 10


class AlwaysOnlineNetwork:
    online = True


@pytest.fixture()
def build(config, events_dir):
    config.ai.enabled = True
    config.ai.snapshot_interval_seconds = 0
    config.ai.recheck_interval_seconds = 60
    config.ai.max_checks = 4
    store = EventStore(events_dir)
    snapshotter = FakeSnapshotter()

    def make(verdicts):
        pipeline = EventPipeline(
            config,
            store,
            MediaMTXClient(config, "svc", "secret"),
            snapshotter,
            AlwaysOnlineNetwork(),
        )
        provider = FakeProvider(verdicts)
        pipeline._ai = provider
        event = store.create("test-cam", config.device.name, utcnow(), 60)
        return pipeline, provider, event, store

    return make


def test_the_first_look_counts_as_a_check(build):
    pipeline, provider, event, _ = build([False])
    pipeline._capture_snapshots(event)
    pipeline._run_ai(event)
    assert provider.calls == 1
    assert event.ai.checks == 1
    assert event.ai.person_detected is False
    assert event.ai.false_positive is False


def test_a_later_check_can_find_a_person_the_first_one_missed(build):
    pipeline, provider, event, store = build([False, True])
    pipeline._capture_snapshots(event)
    pipeline._run_ai(event)
    assert pipeline.recheck(event) is True
    assert provider.calls == 2
    assert event.ai.person_detected is True
    assert event.ai.checks == 2
    assert event.ai.false_positive is False
    assert store.get(event.event_id).ai.person_detected is True


def test_repeated_empty_checks_end_as_a_false_positive(build):
    pipeline, provider, event, _ = build([False])
    pipeline._capture_snapshots(event)
    pipeline._run_ai(event)
    for _ in range(10):
        pipeline.recheck(event)
    assert provider.calls == 4
    assert event.ai.checks == 4
    assert event.ai.false_positive is True


def test_a_found_person_stops_further_checks(build):
    pipeline, provider, event, _ = build([True])
    pipeline._capture_snapshots(event)
    pipeline._run_ai(event)
    assert pipeline.recheck(event) is False
    assert provider.calls == 1


def test_rechecking_is_off_when_the_interval_is_zero(build, config):
    pipeline, provider, event, _ = build([False, True])
    pipeline._capture_snapshots(event)
    pipeline._run_ai(event)
    config.ai.recheck_interval_seconds = 0
    assert pipeline.recheck(event) is False
    assert provider.calls == 1


def test_a_disarmed_camera_is_never_re_checked(build):
    pipeline, provider, event, _ = build([False, True])
    pipeline._capture_snapshots(event)
    pipeline._run_ai(event)
    pipeline._is_armed = lambda: False
    assert pipeline.recheck(event) is False
    assert provider.calls == 1


def test_a_suppressed_notification_is_released_when_a_person_turns_up(build, config):
    config.notifications.enabled = True
    config.notifications.only_if_person = True
    pipeline, _, event, _ = build([False, True])
    pipeline._capture_snapshots(event)
    pipeline._run_ai(event)
    pipeline._notify(event)
    assert event.notification.state == TaskState.SKIPPED.value
    assert "no person" in event.notification.last_error
    pipeline.recheck(event)
    assert "no person" not in event.notification.last_error


def test_each_check_keeps_its_own_frames(build):
    pipeline, _, event, _ = build([False])
    pipeline._capture_snapshots(event)
    pipeline._run_ai(event)
    first = list(event.snapshot.paths)
    pipeline.recheck(event)
    assert len(event.snapshot.paths) > len(first)
    assert any(name.startswith("recheck") for name in event.snapshot.paths)


def test_the_shown_frame_is_the_one_the_verdict_came_from(build):
    pipeline, _, event, store = build([False, True])
    pipeline._capture_snapshots(event)
    pipeline._run_ai(event)
    assert event.snapshot.primary == "snapshot.jpg"
    pipeline.recheck(event)
    assert event.snapshot.primary == "recheck1_1.jpg"
    assert store.get(event.event_id).summary()["snapshot_file"] == "recheck1_1.jpg"


def test_the_first_frame_is_shown_before_any_verdict(build):
    pipeline, _, event, _ = build([False])
    pipeline._capture_snapshots(event)
    assert event.snapshot.primary == "snapshot.jpg"
