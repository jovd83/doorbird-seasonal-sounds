"""Per-device targeting, time-of-day windows, local timestamps, error pages."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.date_logic import matches_time, resolve_active
from app.models import Device, Mp3File, Schedule
from app.timezone import now_local, utc_to_local


def _mp3(i: int, label: str) -> Mp3File:
    return Mp3File(id=i, label=label, filename=f"{label}.mp3", size_bytes=1)


def _sched(**kw) -> Schedule:
    base = dict(
        id=1, name="s", mp3_id=1, start_month=1, start_day=1,
        end_month=12, end_day=31, priority=100, enabled=True,
        start_minute=None, end_minute=None, start_year=None, end_year=None,
    )
    base.update(kw)
    devices = base.pop("devices", [])
    s = Schedule(**base)
    s.devices = devices
    return s


DEFAULT = _mp3(99, "default")


# --- time-of-day --------------------------------------------------------

def test_all_day_schedule_matches_every_minute():
    s = _sched()
    assert matches_time(s, 0) and matches_time(s, 12 * 60) and matches_time(s, 23 * 60 + 59)


@pytest.mark.parametrize("minute,expected", [
    (7 * 60 + 59, False),   # 07:59 just before
    (8 * 60, True),         # 08:00 inclusive start
    (14 * 60, True),
    (22 * 60, True),        # 22:00 inclusive end
    (22 * 60 + 1, False),
])
def test_daytime_window(minute, expected):
    s = _sched(start_minute=8 * 60, end_minute=22 * 60)
    assert matches_time(s, minute) is expected


@pytest.mark.parametrize("minute,expected", [
    (23 * 60, True),        # 23:00 inside the late half
    (1 * 60, True),         # 01:00 inside the early half
    (2 * 60, True),         # 02:00 inclusive end
    (12 * 60, False),       # midday is outside
])
def test_window_wrapping_midnight(minute, expected):
    s = _sched(start_minute=22 * 60, end_minute=2 * 60)
    assert matches_time(s, minute) is expected


def test_time_window_narrows_an_otherwise_matching_schedule():
    night = _sched(id=1, name="night", start_minute=22 * 60, end_minute=23 * 60)
    night.mp3 = _mp3(1, "night-sound")
    at_noon = datetime(2026, 6, 1, 12, 0)
    assert resolve_active([night], DEFAULT, at_noon).mp3 is DEFAULT
    at_night = datetime(2026, 6, 1, 22, 30)
    assert resolve_active([night], DEFAULT, at_night).schedule is night


def test_narrower_time_window_wins_a_priority_tie():
    wide = _sched(id=1, name="wide")
    wide.mp3 = _mp3(1, "wide")
    narrow = _sched(id=2, name="narrow", start_minute=9 * 60, end_minute=10 * 60)
    narrow.mp3 = _mp3(2, "narrow")
    res = resolve_active([wide, narrow], DEFAULT, datetime(2026, 6, 1, 9, 30))
    assert res.schedule is narrow


def test_reason_mentions_the_time_window():
    s = _sched(name="evening", start_minute=18 * 60, end_minute=20 * 60)
    s.mp3 = _mp3(1, "eve")
    res = resolve_active([s], DEFAULT, datetime(2026, 6, 1, 19, 0))
    assert "18:00" in res.reason and "20:00" in res.reason


# --- device targeting ---------------------------------------------------

def test_schedule_with_no_devices_applies_everywhere():
    s = _sched()
    assert s.applies_to(1) and s.applies_to(2) and s.applies_to(None)


def test_schedule_targets_only_its_listed_devices():
    front = Device(id=1, name="front", host="h", username="u", password_enc="x")
    _back = Device(id=2, name="back", host="h", username="u", password_enc="x")
    s = _sched(devices=[front])
    assert s.applies_to(1) is True
    assert s.applies_to(2) is False


def test_resolution_differs_per_device():
    front = Device(id=1, name="front", host="h", username="u", password_enc="x")
    only_front = _sched(id=1, name="front-only", devices=[front])
    only_front.mp3 = _mp3(1, "front-sound")

    when = datetime(2026, 6, 1, 12, 0)
    assert resolve_active([only_front], DEFAULT, when, device_id=1).schedule is only_front
    # The other door falls through to the default.
    assert resolve_active([only_front], DEFAULT, when, device_id=2).mp3 is DEFAULT


def test_targeted_schedule_beats_untargeted_only_on_its_device():
    front = Device(id=1, name="front", host="h", username="u", password_enc="x")
    everywhere = _sched(id=1, name="all", priority=100)
    everywhere.mp3 = _mp3(1, "all-sound")
    just_front = _sched(id=2, name="front", priority=200, devices=[front])
    just_front.mp3 = _mp3(2, "front-sound")

    when = datetime(2026, 6, 1, 12, 0)
    assert resolve_active([everywhere, just_front], DEFAULT, when, 1).schedule is just_front
    assert resolve_active([everywhere, just_front], DEFAULT, when, 2).schedule is everywhere


def test_resolve_still_accepts_a_plain_date():
    """Callers that only know the day must keep working."""
    s = _sched(name="allday")
    s.mp3 = _mp3(1, "x")
    assert resolve_active([s], DEFAULT, date(2026, 6, 1)).schedule is s


# --- local time ---------------------------------------------------------

def test_now_local_is_naive_and_near_brussels_wall_clock():
    now = now_local()
    assert now.tzinfo is None
    utc = datetime.now(UTC).replace(tzinfo=None)
    offset_hours = round((now - utc).total_seconds() / 3600)
    assert offset_hours in (1, 2), f"expected CET/CEST, got {offset_hours:+d}h"


def test_utc_to_local_shifts_forward():
    converted = utc_to_local(datetime(2026, 8, 22, 10, 0))
    assert converted.hour in (11, 12)      # CET or CEST
