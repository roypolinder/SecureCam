import pytest

from securecam.events import EventStore, TaskState
from securecam.mediamtx import MediaMTXClient
from securecam.networking import NetworkMonitor
from securecam.pipeline import EventPipeline
from securecam.snapshot import SnapshotCapturer
from securecam.util import utcnow


@pytest.fixture()
def pipeline_factory(config, events_dir):
    store = EventStore(events_dir)
    client = MediaMTXClient(config, "svc", "secret")
    snapshotter = SnapshotCapturer(config, lambda: ("user", "pass"))
    network = NetworkMonitor(config.network)

    def build(armed):
        return store, EventPipeline(config, store, client, snapshotter, network, is_armed=lambda: armed)

    return build


def test_ai_is_not_called_while_disarmed(pipeline_factory, config):
    config.ai.enabled = True
    store, pipeline = pipeline_factory(armed=False)
    event = store.create("test-cam", config.device.name, utcnow(), 60)
    pipeline._run_ai(event)
    assert event.ai.state == TaskState.SKIPPED.value
    assert "disarmed" in event.ai.last_error


def test_notifications_are_not_sent_while_disarmed(pipeline_factory, config):
    config.notifications.enabled = True
    store, pipeline = pipeline_factory(armed=False)
    event = store.create("test-cam", config.device.name, utcnow(), 60)
    pipeline._notify(event)
    assert event.notification.state == TaskState.SKIPPED.value
    assert "disarmed" in event.notification.last_error


def test_an_armed_pipeline_still_reaches_the_ai_step(pipeline_factory, config):
    config.ai.enabled = False
    store, pipeline = pipeline_factory(armed=True)
    event = store.create("test-cam", config.device.name, utcnow(), 60)
    pipeline._run_ai(event)
    assert event.ai.state == TaskState.SKIPPED.value
    assert "disabled" in event.ai.last_error
