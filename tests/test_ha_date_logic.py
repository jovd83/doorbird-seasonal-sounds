"""Verify the HA integration's date_logic gives the same answers as the
sibling Docker app's date_logic — they're meant to be drop-in equivalent."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "home_assistant" / "custom_components" / "doorbird_seasonal"),
)

from date_logic import Schedule as HaSchedule
from date_logic import matches_today, resolve_active


def _ha(name, **kw):
    yr = kw.get("year")
    return HaSchedule(
        name=name,
        mp3=kw.get("mp3", "x.mp3"),
        start_month=kw["start_month"],
        start_day=kw["start_day"],
        end_month=kw.get("end_month"),
        end_day=kw.get("end_day"),
        start_year=kw.get("start_year", yr),
        end_year=kw.get("end_year", yr),
        priority=kw.get("priority", 100),
    )


def test_ha_single_day_match():
    s = _ha("xmas", start_month=12, start_day=25)
    assert matches_today(s, date(2026, 12, 25))
    assert not matches_today(s, date(2026, 12, 24))


def test_ha_year_wrap():
    s = _ha("xmas", start_month=12, start_day=20, end_month=1, end_day=6)
    assert matches_today(s, date(2026, 12, 25))
    assert matches_today(s, date(2026, 1, 1))
    assert not matches_today(s, date(2026, 1, 7))


def test_ha_one_off_year():
    s = _ha("easter26", start_month=4, start_day=3, end_month=4, end_day=6, year=2026)
    assert matches_today(s, date(2026, 4, 5))
    assert not matches_today(s, date(2025, 4, 5))


def test_ha_one_off_multi_year_wrap():
    s = _ha("ny2627", start_month=12, start_day=30, end_month=1, end_day=2,
            start_year=2026, end_year=2027)
    assert matches_today(s, date(2026, 12, 31))
    assert matches_today(s, date(2027, 1, 1))
    assert not matches_today(s, date(2027, 1, 3))
    assert not matches_today(s, date(2025, 12, 31))


def test_ha_priority_wins():
    a = _ha("winter", start_month=12, start_day=1, end_month=2, end_day=28,
            priority=100, mp3="winter.mp3")
    b = _ha("xmas", start_month=12, start_day=20, end_month=1, end_day=6,
            priority=200, mp3="xmas.mp3")
    res = resolve_active([a, b], "default.mp3", date(2026, 12, 25))
    assert res.mp3 == "xmas.mp3"
    res2 = resolve_active([a, b], "default.mp3", date(2026, 12, 5))
    assert res2.mp3 == "winter.mp3"


def test_ha_falls_back_to_default():
    res = resolve_active([], "default.mp3", date(2026, 7, 1))
    assert res.mp3 == "default.mp3"
    assert res.schedule is None
