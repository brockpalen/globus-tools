"""Formatting helpers and the derived task fields."""

import datetime as dt

import pytest

from globus_tools import common
from globus_tools import tasks as tasklib


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "-"),
        (0, "0B"),
        (999, "999B"),
        (1000, "1.0K"),
        (1500, "1.5K"),
        (100 * 1000**2, "100.0M"),
        (2 * 1000**3, "2.0G"),
        (100 * 1000**4, "100.0T"),
    ],
)
def test_human_bytes(value, expected):
    """Sizes use decimal units, the way transfer rates are quoted."""
    assert common.human_bytes(value) == expected


def test_human_bytes_suffix():
    """A rate carries its per-second suffix."""
    assert common.human_bytes(100 * 1000**2, "/s") == "100.0M/s"


@pytest.mark.parametrize(
    "seconds,expected",
    [(None, "-"), (5, "5s"), (65, "1m05s"), (3700, "1h01m"), (90000, "1d1h")],
)
def test_human_dur(seconds, expected):
    """Durations show the two most significant units."""
    assert common.human_dur(seconds) == expected


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-01T12:00:00+00:00",
        "2026-09-01 12:00:00+00:00",
        "2026-09-01T12:00:00Z",
        "2026-09-01T12:00:00",
    ],
)
def test_parse_time_accepts_every_form_the_api_returns(value):
    """Space or T separated, with or without a zone, defaulting to UTC."""
    parsed = common.parse_time(value)
    assert parsed.tzinfo is not None
    assert parsed.year == 2026


@pytest.mark.parametrize("value", [None, "", "not a time"])
def test_parse_time_rejects_junk(value):
    """Unparseable input is None rather than an exception."""
    assert common.parse_time(value) is None


def test_age_seconds():
    """Age is measured from now, in seconds."""
    then = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    assert 595 < common.age_seconds(then.isoformat()) < 605


@pytest.mark.parametrize(
    "task,expected",
    [
        ({"status": "ACTIVE", "nice_status": "OK"}, ""),
        ({"status": "ACTIVE", "nice_status": "Queued"}, ""),
        ({"status": "ACTIVE", "nice_status": None}, ""),
        ({"status": "ACTIVE", "is_paused": True}, "PAUSED"),
        ({"status": "ACTIVE", "nice_status": "PERMISSION_DENIED"}, "PERMISSION_DENIED"),
        ({"status": "INACTIVE"}, "INACTIVE"),
    ],
)
def test_issue_of(task, expected):
    """The ISSUE column is the one operators grep for."""
    assert tasklib.issue_of(task) == expected


def test_paused_beats_nice_status():
    """A paused task reports PAUSED even when it also has a nice_status."""
    assert (
        tasklib.issue_of({"is_paused": True, "nice_status": "ENDPOINT_ERROR"})
        == "PAUSED"
    )


@pytest.mark.parametrize(
    "task,expected",
    [
        ({"status": "ACTIVE", "nice_status": "OK", "faults": 0}, False),
        ({"status": "ACTIVE", "nice_status": "OK", "faults": 3}, True),
        ({"status": "ACTIVE", "nice_status": "CONNECT_FAILED"}, True),
        ({"status": "ACTIVE", "is_paused": True}, True),
    ],
)
def test_is_problem(task, expected):
    """--errors selects faulting, paused or badly-statused tasks."""
    assert tasklib.is_problem(task) is expected
