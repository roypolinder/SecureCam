from datetime import timedelta

from securecam.events import EventStatus, EventStore
from securecam.storage import StorageManager
from securecam.util import to_rfc3339, utcnow


def make(config):
    store = EventStore(config.storage.events_path)
    manager = StorageManager(config, store)
    manager.prepare()
    return store, manager


def add_event(store, age_days, size_bytes=1024):
    created = utcnow() - timedelta(days=age_days)
    event = store.create("cam-1", "Front Door", created, 60)
    event.status = EventStatus.COMPLETED.value
    event.created_at = to_rfc3339(created)
    store.save(event)
    with open(event.recording_path, "wb") as handle:
        handle.write(b"0" * size_bytes)
    return event


def test_prepare_creates_the_directories(config):
    import os

    _, manager = make(config)
    assert os.path.isdir(config.storage.events_path)
    assert os.path.isdir(config.storage.buffer_path)


def test_retention_deletes_old_events(config):
    store, manager = make(config)
    old = add_event(store, age_days=config.storage.retention_days + 2)
    fresh = add_event(store, age_days=0)
    result = manager.enforce()
    assert result.deleted_events == 1
    assert store.get(old.event_id) is None
    assert store.get(fresh.event_id) is not None


def test_retention_keeps_everything_inside_the_window(config):
    store, manager = make(config)
    add_event(store, age_days=1)
    add_event(store, age_days=2)
    assert manager.enforce().deleted_events == 0


def test_active_events_are_never_deleted(config):
    store, manager = make(config)
    event = store.create("cam-1", "Front Door", utcnow() - timedelta(days=99), 60)
    manager.enforce()
    assert store.get(event.event_id) is not None


def test_status_reports_writability(config):
    _, manager = make(config)
    status = manager.status()
    assert status.writable is True
    assert status.free_bytes > 0


def test_recover_interrupted_marks_orphaned_events(config):
    store, manager = make(config)
    event = store.create("cam-1", "Front Door", utcnow(), 60)
    assert manager.recover_interrupted() == [event.event_id]
    assert store.get(event.event_id).status == EventStatus.INTERRUPTED.value


def test_recover_interrupted_is_idempotent(config):
    store, manager = make(config)
    store.create("cam-1", "Front Door", utcnow(), 60)
    manager.recover_interrupted()
    assert manager.recover_interrupted() == []
