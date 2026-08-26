from datetime import timedelta

import pytest

from securecam.events import EventStatus, EventStore, TaskInfo, TaskState
from securecam.util import to_rfc3339, utcnow


def store(events_dir):
    return EventStore(events_dir)


def test_create_writes_metadata_immediately(events_dir):
    event = store(events_dir).create("cam-1", "Front Door", utcnow(), pre_event_seconds=60)
    assert event.event_id.startswith("event_")
    assert event.status == EventStatus.RECORDING.value
    import os

    assert os.path.isfile(event.metadata_path)


def test_clip_start_includes_the_pre_event_window(events_dir):
    started = utcnow()
    event = store(events_dir).create("cam-1", "Front Door", started, pre_event_seconds=90)
    from securecam.util import parse_rfc3339

    assert (started - parse_rfc3339(event.clip_start_at)).total_seconds() == pytest.approx(90, abs=0.01)


def test_round_trip_through_disk(events_dir):
    events = store(events_dir)
    event = events.create("cam-1", "Front Door", utcnow(), 60)
    event.ai.person_detected = True
    event.ai.confidence = 0.91
    event.status = EventStatus.COMPLETED.value
    events.save(event)

    loaded = events.get(event.event_id)
    assert loaded is not None
    assert loaded.ai.person_detected is True
    assert loaded.ai.confidence == 0.91
    assert loaded.status == EventStatus.COMPLETED.value


def test_list_filters_by_person(events_dir):
    events = store(events_dir)
    for index in range(3):
        event = events.create("cam-1", "Front Door", utcnow() + timedelta(seconds=index), 60)
        event.status = EventStatus.COMPLETED.value
        event.ai.person_detected = index == 1
        events.save(event)
    assert len(events.list()) == 3
    assert len(events.list(person_only=True)) == 1


def test_active_events_cannot_be_deleted(events_dir):
    events = store(events_dir)
    event = events.create("cam-1", "Front Door", utcnow(), 60)
    try:
        events.delete(event.event_id)
    except ValueError as exc:
        assert "cannot be deleted" in str(exc)
    else:
        raise AssertionError("deleting an active event should raise")


def test_completed_events_can_be_deleted(events_dir):
    events = store(events_dir)
    event = events.create("cam-1", "Front Door", utcnow(), 60)
    event.status = EventStatus.COMPLETED.value
    events.save(event)
    assert events.delete(event.event_id) is True
    assert events.get(event.event_id) is None


def test_pending_finds_events_whose_retry_is_due(events_dir):
    events = store(events_dir)
    event = events.create("cam-1", "Front Door", utcnow(), 60)
    event.status = EventStatus.COMPLETED.value
    event.ai.fail("temporary failure", retry_in=-1)
    event.notification.skip()
    event.recording.succeed()
    events.save(event)
    assert [e.event_id for e in events.pending()] == [event.event_id]


def test_pending_ignores_scheduled_retries_in_the_future(events_dir):
    events = store(events_dir)
    event = events.create("cam-1", "Front Door", utcnow(), 60)
    event.status = EventStatus.COMPLETED.value
    event.ai.fail("temporary failure", retry_in=3600)
    event.notification.skip()
    event.recording.succeed()
    events.save(event)
    assert events.pending() == []


def test_task_states():
    task = TaskInfo()
    assert task.due is True
    task.begin()
    assert task.attempts == 1
    task.fail("boom", retry_in=3600)
    assert task.state == TaskState.PENDING.value
    assert task.due is False
    task.give_up("permanent")
    assert task.state == TaskState.FAILED.value
    assert task.due is False
    task.succeed()
    assert task.state == TaskState.COMPLETED.value
    assert task.last_error == ""


def test_unreadable_metadata_is_skipped_not_fatal(events_dir):
    import os

    events = store(events_dir)
    event = events.create("cam-1", "Front Door", utcnow(), 60)
    with open(event.metadata_path, "w", encoding="utf-8") as handle:
        handle.write("{ not json")
    assert list(events.iter_events()) == []


def test_summary_is_json_safe(events_dir):
    import json

    events = store(events_dir)
    event = events.create("cam-1", "Front Door", utcnow(), 60)
    event.ended_at = to_rfc3339(utcnow())
    json.dumps(event.summary())
