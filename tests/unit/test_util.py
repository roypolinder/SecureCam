import pytest

from securecam.util import (
    Backoff,
    deep_merge,
    dotted_get,
    format_duration,
    human_bytes,
    parse_duration_seconds,
    parse_rfc3339,
    to_rfc3339,
    utcnow,
)


def test_rfc3339_round_trip():
    now = utcnow()
    assert abs((parse_rfc3339(to_rfc3339(now)) - now).total_seconds()) < 0.01


def test_parse_rfc3339_accepts_offsets():
    assert parse_rfc3339("2024-01-01T12:00:00+02:00").hour == 10


def test_parse_rfc3339_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_rfc3339("yesterday")


@pytest.mark.parametrize(
    "text,expected",
    [("30", 30), ("30s", 30), ("5m", 300), ("2h", 7200), ("1d", 86400), ("1.5m", 90)],
)
def test_parse_duration(text, expected):
    assert parse_duration_seconds(text) == expected


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration_seconds("soon")


def test_format_duration_is_readable():
    assert format_duration(45) == "45s"
    assert format_duration(90) == "1m 30s"
    assert format_duration(3720) == "1h 2m"


def test_human_bytes():
    assert human_bytes(512) == "512 B"
    assert human_bytes(1536).startswith("1.5")
    assert human_bytes(3 * 1024 ** 3).endswith("GiB")


def test_deep_merge_prefers_the_override():
    base = {"a": 1, "nested": {"x": 1, "y": 2}}
    assert deep_merge(base, {"nested": {"y": 9}, "b": 2}) == {"a": 1, "b": 2, "nested": {"x": 1, "y": 9}}


def test_deep_merge_does_not_mutate_the_base():
    base = {"nested": {"x": 1}}
    deep_merge(base, {"nested": {"x": 2}})
    assert base["nested"]["x"] == 1


def test_dotted_get():
    payload = {"choices": [{"message": {"content": "hello"}}]}
    assert dotted_get(payload, "choices.0.message.content") == "hello"
    assert dotted_get(payload, "choices.5.message", "fallback") == "fallback"
    assert dotted_get(payload, "missing") is None


def test_backoff_grows_then_caps():
    backoff = Backoff(initial=1.0, maximum=8.0, jitter=0.0)
    assert [backoff.next_delay() for _ in range(5)] == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_backoff_resets():
    backoff = Backoff(initial=1.0, maximum=8.0, jitter=0.0)
    backoff.next_delay()
    backoff.next_delay()
    backoff.reset()
    assert backoff.next_delay() == 1.0
