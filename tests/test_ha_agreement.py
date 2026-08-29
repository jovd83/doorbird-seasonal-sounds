"""The app and the Home Assistant component must resolve schedules identically.

The HA custom component is deployed on its own into HA's config directory and
cannot import from the app, so it carries a vendored copy of the schedule
rules. Vendoring is fine; *diverging* is not, and it had: the HA copy had no
concept of a time-of-day window, so any schedule using one resolved one way in
the app and another way on the HA sensor, with nothing to say which was right.

This runs both implementations over the same fixtures and fails on the first
disagreement. If a rule changes in one file, it has to change in the other.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from app import date_logic as app_logic
from app.models import Mp3File
from app.models import Schedule as AppSchedule

# The component directory itself, not its parent: importing the package would
# execute `__init__.py`, which needs Home Assistant and voluptuous installed.
# The vendored rules are deliberately free of both.
_HA_COMPONENT = (
    Path(__file__).resolve().parents[1]
    / "home_assistant" / "custom_components" / "doorbird_seasonal"
)
if str(_HA_COMPONENT) not in sys.path:
    sys.path.insert(0, str(_HA_COMPONENT))

import date_logic as ha_logic


def _app_schedule(**kw) -> AppSchedule:
    """An unsaved ORM object. Never added to a session; the rules are pure."""
    label = kw.pop("label", "sound")
    fields = {
        "id": kw.pop("id", 1),
        "name": "s",
        "kind": "chime",
        "mp3_id": 1,
        "collection_id": None,
        "start_month": 1, "start_day": 1,
        "end_month": None, "end_day": None,
        "start_year": None, "end_year": None,
        "priority": 100,
        "enabled": True,
        "start_minute": None, "end_minute": None,
        "delay_seconds": 0,
    }
    fields.update(kw)
    s = AppSchedule(**fields)
    s.mp3 = Mp3File(id=fields["mp3_id"], label=label, filename="x.mp3", size_bytes=1)
    return s


def _ha_schedule(**kw) -> ha_logic.Schedule:
    fields = {
        "name": "s",
        "mp3": kw.pop("label", "sound"),
        "start_month": 1, "start_day": 1,
        "end_month": None, "end_day": None,
        "start_year": None, "end_year": None,
        "priority": 100,
        "start_minute": None, "end_minute": None,
        "enabled": True,
    }
    kw.pop("id", None)
    kw.pop("kind", None)
    kw.pop("mp3_id", None)
    kw.pop("collection_id", None)
    kw.pop("delay_seconds", None)
    fields.update(kw)
    return ha_logic.Schedule(**fields)


# name -> the keyword arguments both builders understand. The keyword form
# keeps these readable as a table.
FIXTURES: dict[str, dict] = {
    "all-year":        dict(start_month=1, start_day=1, end_month=12, end_day=31),
    "christmas":       dict(start_month=12, start_day=20, end_month=1, end_day=6, priority=200),
    "single-day":      dict(start_month=7, start_day=14, priority=300),
    "one-off-2026":    dict(start_month=3, start_day=1, end_month=3, end_day=8,
                            start_year=2026, end_year=2026, priority=150),
    "multi-year":      dict(start_month=11, start_day=1, end_month=2, end_day=1,
                            start_year=2026, end_year=2027, priority=120),
    "evenings":        dict(start_month=1, start_day=1, end_month=12, end_day=31,
                            start_minute=18 * 60, end_minute=22 * 60, priority=250),
    "small-hours":     dict(start_month=1, start_day=1, end_month=12, end_day=31,
                            start_minute=22 * 60, end_minute=2 * 60, priority=250),
    "disabled":        dict(start_month=1, start_day=1, end_month=12, end_day=31,
                            enabled=False, priority=999),
    "leap-day":        dict(start_month=2, start_day=29, priority=400),
}

MOMENTS = [
    datetime(2026, 1, 3, 12, 0),
    datetime(2026, 1, 3, 19, 30),
    datetime(2026, 1, 3, 23, 30),
    datetime(2026, 1, 3, 1, 0),
    datetime(2026, 3, 5, 9, 0),
    datetime(2026, 7, 14, 15, 0),
    datetime(2026, 12, 25, 20, 0),
    datetime(2027, 1, 15, 6, 0),
    datetime(2028, 2, 29, 11, 0),
    datetime(2026, 6, 1, 0, 0),
    datetime(2026, 6, 1, 23, 59),
]


@pytest.mark.parametrize("fixture_name", sorted(FIXTURES))
@pytest.mark.parametrize("moment", MOMENTS, ids=lambda m: m.isoformat())
def test_date_matching_agrees(fixture_name, moment):
    """Both must agree on whether one schedule is active at one moment."""
    kw = FIXTURES[fixture_name]
    app_s = _app_schedule(name=fixture_name, **kw)
    ha_s = _ha_schedule(name=fixture_name, **kw)

    app_answer = app_logic.matches_now(app_s, moment)
    ha_answer = ha_logic.matches_now(ha_s, moment)
    assert app_answer == ha_answer, (
        f"{fixture_name} at {moment}: app says {app_answer}, HA says {ha_answer}")


@pytest.mark.parametrize("moment", MOMENTS, ids=lambda m: m.isoformat())
def test_the_same_schedule_wins_in_both(moment):
    """With every fixture competing, both must pick the same winner."""
    app_schedules = [
        _app_schedule(id=i, name=n, label=n, **kw)
        for i, (n, kw) in enumerate(sorted(FIXTURES.items()), start=1)
    ]
    ha_schedules = [
        _ha_schedule(name=n, label=n, **kw)
        for n, kw in sorted(FIXTURES.items())
    ]

    app_winner = app_logic.pick_schedule(app_schedules, moment)
    ha_winner = ha_logic.pick_schedule(ha_schedules, moment)

    app_name = app_winner.name if app_winner else None
    ha_name = ha_winner.name if ha_winner else None
    assert app_name == ha_name, (
        f"at {moment}: app picks {app_name!r}, HA picks {ha_name!r}")


@pytest.mark.parametrize("moment", MOMENTS, ids=lambda m: m.isoformat())
def test_the_resolved_sound_agrees(moment):
    """And the resolution, including the fallback to the default."""
    app_schedules = [
        _app_schedule(id=i, name=n, label=n, **kw)
        for i, (n, kw) in enumerate(sorted(FIXTURES.items()), start=1)
    ]
    ha_schedules = [
        _ha_schedule(name=n, label=n, **kw)
        for n, kw in sorted(FIXTURES.items())
    ]
    default = Mp3File(id=99, label="default", filename="d.mp3", size_bytes=1)

    app_res = app_logic.resolve_active(app_schedules, default, moment)
    ha_res = ha_logic.resolve_active(ha_schedules, "default", moment)

    assert app_res.mp3.label == ha_res.mp3, f"at {moment}: {app_res.mp3.label} vs {ha_res.mp3}"


def test_the_ha_copy_supports_time_windows_at_all():
    """The specific gap this file was written to close."""
    evening = ha_logic.Schedule(
        name="evenings", mp3="x", start_month=1, start_day=1,
        end_month=12, end_day=31, start_minute=18 * 60, end_minute=22 * 60,
    )
    assert ha_logic.matches_now(evening, datetime(2026, 6, 1, 19, 0))
    assert not ha_logic.matches_now(evening, datetime(2026, 6, 1, 9, 0))


def test_both_expose_the_same_public_rules():
    """A rule added to one file has to be added to the other."""
    expected = {
        "matches_date", "matches_time", "matches_today", "matches_now",
        "pick_schedule", "resolve_active", "describe", "MINUTES_PER_DAY",
    }
    missing_in_ha = {n for n in expected if not hasattr(ha_logic, n)}
    missing_in_app = {n for n in expected if not hasattr(app_logic, n)}
    assert not missing_in_ha, f"the HA copy is missing {missing_in_ha}"
    assert not missing_in_app, f"the app is missing {missing_in_app}"


def test_date_only_helpers_agree():
    """`matches_today` ignores the clock in both."""
    for name, kw in sorted(FIXTURES.items()):
        app_s, ha_s = _app_schedule(name=name, **kw), _ha_schedule(name=name, **kw)
        for day in (date(2026, 1, 3), date(2026, 7, 14), date(2026, 12, 25)):
            assert app_logic.matches_today(app_s, day) == ha_logic.matches_today(ha_s, day), \
                f"{name} on {day}"
