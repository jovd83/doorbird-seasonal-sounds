from datetime import date

from app.date_logic import matches_today, resolve_active


class _Stub:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.name = kw.get("name", "s")
        self.start_month = kw["start_month"]
        self.start_day = kw["start_day"]
        self.end_month = kw.get("end_month")
        self.end_day = kw.get("end_day")
        self.start_year = kw.get("start_year", kw.get("year"))
        self.end_year = kw.get("end_year", kw.get("year"))
        self.priority = kw.get("priority", 100)
        self.enabled = kw.get("enabled", True)
        self.mp3 = kw.get("mp3")
        # Mirror the real model: all-day and untargeted unless stated.
        self.start_minute = kw.get("start_minute")
        self.end_minute = kw.get("end_minute")
        self.devices = kw.get("devices", [])
        # Every day, no holiday rule -- the shape of a schedule written before
        # those existed, and what the migration gives every one of them.
        self.weekday_mask = kw.get("weekday_mask", 0b1111111)
        self.skip_public_holidays = kw.get("skip_public_holidays", False)
        self.holiday_keys = frozenset(kw.get("holiday_keys", ()))

    @property
    def all_day(self) -> bool:
        return self.start_minute is None and self.end_minute is None

    def applies_to(self, device_id) -> bool:
        if not self.devices or device_id is None:
            return True
        return any(d.id == device_id for d in self.devices)


class _Mp3:
    def __init__(self, label="default"):
        self.id = id(self)
        self.label = label


def test_single_day_recurring_matches_that_day():
    s = _Stub(start_month=12, start_day=25)
    assert matches_today(s, date(2026, 12, 25))
    assert not matches_today(s, date(2026, 12, 24))
    assert not matches_today(s, date(2026, 12, 26))


def test_range_within_a_year():
    s = _Stub(start_month=6, start_day=21, end_month=9, end_day=22)
    assert matches_today(s, date(2026, 8, 1))
    assert matches_today(s, date(2026, 6, 21))
    assert matches_today(s, date(2026, 9, 22))
    assert not matches_today(s, date(2026, 9, 23))
    assert not matches_today(s, date(2026, 6, 20))


def test_range_wraps_year_end():
    s = _Stub(start_month=12, start_day=20, end_month=1, end_day=6)
    assert matches_today(s, date(2026, 12, 25))
    assert matches_today(s, date(2026, 1, 1))
    assert matches_today(s, date(2026, 1, 6))
    assert not matches_today(s, date(2026, 1, 7))
    assert not matches_today(s, date(2026, 12, 19))


def test_one_off_year_bound():
    s = _Stub(start_month=8, start_day=15, end_month=8, end_day=20, year=2026)
    assert matches_today(s, date(2026, 8, 17))
    assert not matches_today(s, date(2025, 8, 17))
    assert not matches_today(s, date(2027, 8, 17))


def test_one_off_multi_year_wrap():
    s = _Stub(start_month=12, start_day=20, end_month=1, end_day=6,
              start_year=2026, end_year=2027)
    assert matches_today(s, date(2026, 12, 25))
    assert matches_today(s, date(2027, 1, 6))
    assert not matches_today(s, date(2027, 1, 7))
    assert not matches_today(s, date(2025, 12, 25))


def test_one_off_multi_year_only_that_window():
    s = _Stub(start_month=12, start_day=20, end_month=1, end_day=6,
              start_year=2026, end_year=2027)
    # The matching window is 2026-12-20 → 2027-01-06. No other years.
    assert not matches_today(s, date(2028, 12, 25))
    assert not matches_today(s, date(2028, 1, 3))


def test_disabled_never_matches():
    s = _Stub(start_month=12, start_day=25, enabled=False)
    assert not matches_today(s, date(2026, 12, 25))


def test_resolve_picks_higher_priority():
    default = _Mp3("default")
    xmas_mp3 = _Mp3("xmas")
    winter_mp3 = _Mp3("winter")
    xmas = _Stub(start_month=12, start_day=20, end_month=1, end_day=6,
                 priority=200, mp3=xmas_mp3, name="xmas")
    winter = _Stub(start_month=12, start_day=1, end_month=2, end_day=28,
                   priority=100, mp3=winter_mp3, name="winter")
    res = resolve_active([xmas, winter], default, date(2026, 12, 25))
    assert res.mp3 is xmas_mp3
    res2 = resolve_active([xmas, winter], default, date(2026, 12, 5))
    assert res2.mp3 is winter_mp3
    res3 = resolve_active([xmas, winter], default, date(2026, 3, 15))
    assert res3.mp3 is default
    assert res3.schedule is None


def test_resolve_uses_default_when_no_match():
    default = _Mp3("default")
    res = resolve_active([], default, date(2026, 7, 1))
    assert res.mp3 is default
    assert res.schedule is None
    assert "default" in res.reason


def test_recurring_fires_every_year_for_two_decades():
    """A recurring schedule has no year stored, so it MUST keep firing
    every calendar year indefinitely. This test pins that contract.
    """
    s = _Stub(start_month=12, start_day=20, end_month=1, end_day=6)
    for year in range(2026, 2046):
        assert matches_today(s, date(year, 12, 25)), f"missed Christmas {year}"
        assert matches_today(s, date(year + 1, 1, 1)), f"missed NYD {year + 1}"
        assert not matches_today(s, date(year, 2, 1)), f"falsely matched Feb {year}"


def test_recurring_unaffected_by_leap_year():
    s = _Stub(start_month=2, start_day=29)
    # leap years
    assert matches_today(s, date(2028, 2, 29))
    assert matches_today(s, date(2032, 2, 29))
    # non-leap years: Feb 29 doesn't exist, so it just never matches that year — no crash
    assert not matches_today(s, date(2027, 2, 28))
    assert not matches_today(s, date(2029, 3, 1))


def test_recurring_window_unchanged_across_years():
    """For any (today.month, today.day) inside the window, year doesn't matter."""
    summer = _Stub(start_month=6, start_day=21, end_month=9, end_day=22)
    for year in (2026, 2030, 2040, 2099):
        assert matches_today(summer, date(year, 7, 15))
        assert matches_today(summer, date(year, 6, 21))
        assert matches_today(summer, date(year, 9, 22))
        assert not matches_today(summer, date(year, 6, 20))
        assert not matches_today(summer, date(year, 9, 23))
