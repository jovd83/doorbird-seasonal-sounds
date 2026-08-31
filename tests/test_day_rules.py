"""The weekday / holiday rule, against the real model objects.

Every date here is a real one in 2026–2027, chosen so the case being tested is
the *only* reason the answer comes out the way it does. Where that matters the
weekday is asserted first, so a test that starts failing because someone
misread a calendar says so out loud.
"""
from datetime import date, datetime

import pytest

from app import holidays
from app.date_logic import (
    describe_days,
    find_next_change,
    matches_day,
    matches_today,
    pick_schedule,
)
from app.models import Mp3File, Schedule

# Reference dates, with the weekday that makes each one interesting.
SAT_29_AUG = date(2026, 8, 29)
SUN_30_AUG = date(2026, 8, 30)
MON_31_AUG = date(2026, 8, 31)
WED_11_NOV = date(2026, 11, 11)   # Armistice Day — public, on a weekday
SUN_6_DEC = date(2026, 12, 6)     # Sinterklaas — observance, on a weekend
THU_31_DEC = date(2026, 12, 31)   # New Year's Eve — observance, on a weekday
FRI_25_DEC = date(2026, 12, 25)   # Christmas Day — public, on a weekday
FRI_1_JAN = date(2027, 1, 1)      # New Year's Day — public, on a weekday


def _sched(**kw) -> Schedule:
    """An unsaved Schedule. Never added to a session; the rules are pure."""
    keys = kw.pop("holiday_keys", ())
    fields = {
        "id": kw.pop("id", 1),
        "name": kw.pop("name", "s"),
        "kind": "chime",
        "mp3_id": 1,
        "collection_id": None,
        # All year round, so only the day rule can ever decide the answer.
        "start_month": 1, "start_day": 1, "end_month": 12, "end_day": 31,
        "start_year": None, "end_year": None,
        "priority": 100,
        "enabled": True,
        "start_minute": None, "end_minute": None,
        "delay_seconds": 0,
        "weekday_mask": holidays.ALL_DAYS,
        "skip_public_holidays": False,
        "date_mode": holidays.DATE_RANGE,
    }
    fields.update(kw)
    s = Schedule(**fields)
    s.mp3 = Mp3File(id=1, label=fields["name"], filename="x.mp3", size_bytes=1)
    s.set_holiday_keys(keys)
    return s


def test_the_reference_dates_are_the_weekdays_this_file_claims():
    assert SAT_29_AUG.weekday() == holidays.SATURDAY
    assert MON_31_AUG.weekday() == holidays.MONDAY
    assert WED_11_NOV.weekday() == holidays.WEDNESDAY
    assert SUN_6_DEC.weekday() == holidays.SUNDAY
    assert THU_31_DEC.weekday() == holidays.THURSDAY
    assert FRI_25_DEC.weekday() == holidays.FRIDAY
    assert FRI_1_JAN.weekday() == holidays.FRIDAY


# ------------------------------------------------------------- weekdays only


def test_every_day_is_the_default_and_matches_everything():
    s = _sched()
    assert all(matches_day(s, d) for d in (SAT_29_AUG, MON_31_AUG, FRI_25_DEC))


def test_weekdays_preset_excludes_the_weekend():
    s = _sched(weekday_mask=holidays.WEEKDAYS)
    assert matches_day(s, MON_31_AUG)
    assert not matches_day(s, SAT_29_AUG)
    assert not matches_day(s, SUN_30_AUG)


def test_weekend_preset_is_the_complement():
    s = _sched(weekday_mask=holidays.WEEKEND)
    assert matches_day(s, SAT_29_AUG)
    assert not matches_day(s, MON_31_AUG)


# -------------------------------------------------- holidays widen the match


def test_holidays_mode_ignores_the_weekday_rule_entirely():
    """The mode names the exact days; a weekday rule could only remove one.

    Sinterklaas 2026 falls on a Sunday. The stored mask still says Mo-Fr --
    switching modes does not throw it away -- but nothing consults it here.
    """
    s = _sched(date_mode=holidays.DATE_HOLIDAYS,
               weekday_mask=holidays.WEEKDAYS, holiday_keys=("sinterklaas",))
    assert matches_day(s, SUN_6_DEC)
    # `matches_day` is unconditionally true in this mode; the holidays are
    # what `matches_date` checks, so the pair is what decides.
    assert matches_today(s, SUN_6_DEC)
    assert not matches_today(s, SUN_30_AUG)


def test_holidays_mode_never_fires_on_an_ordinary_day():
    s = _sched(date_mode=holidays.DATE_HOLIDAYS,
               holiday_keys=("christmas", "sinterklaas"))
    assert matches_today(s, FRI_25_DEC)
    assert matches_today(s, SUN_6_DEC)
    assert not matches_today(s, MON_31_AUG)
    assert not matches_today(s, WED_11_NOV)   # a holiday, but not a ticked one


def test_a_range_schedule_ignores_its_holiday_ticks():
    """The three modes are exclusive, so a range never consults them.

    The rows are deleted by the migration, but a hand-edited database or an
    older client could still present the pair, and the answer must not depend
    on which one you look at.
    """
    s = _sched(date_mode=holidays.DATE_RANGE,
               weekday_mask=holidays.WEEKDAYS, holiday_keys=("sinterklaas",))
    assert not matches_day(s, SUN_6_DEC)


# ------------------------------------------------------- the one subtraction


def test_skip_removes_public_holidays_from_a_weekday_rule():
    s = _sched(weekday_mask=holidays.WEEKDAYS, skip_public_holidays=True)
    assert matches_day(s, MON_31_AUG)
    assert not matches_day(s, WED_11_NOV)   # Armistice Day
    assert not matches_day(s, FRI_25_DEC)   # Christmas Day
    assert not matches_day(s, FRI_1_JAN)    # New Year's Day


def test_skip_leaves_community_days_and_observances_alone():
    """New Year's Eve is a Thursday and an observance, so it survives."""
    s = _sched(weekday_mask=holidays.WEEKDAYS, skip_public_holidays=True)
    assert matches_day(s, THU_31_DEC)
    assert not holidays.is_public_holiday(THU_31_DEC)


def test_the_skip_is_inert_in_holidays_mode():
    """Nothing matched by weekday there, so there is nothing to subtract.

    It is also unreachable from the form -- the Days control is greyed out --
    but a stored true must not be able to cancel the very day that was named.
    """
    s = _sched(date_mode=holidays.DATE_HOLIDAYS, skip_public_holidays=True,
               holiday_keys=("christmas",))
    assert matches_today(s, FRI_25_DEC)


def test_skip_does_nothing_without_a_weekday_to_subtract_from():
    s = _sched(weekday_mask=holidays.NO_DAYS, skip_public_holidays=True)
    assert not matches_day(s, FRI_25_DEC)
    assert not matches_day(s, MON_31_AUG)


# ------------------------------------------------------ against the calendar


def test_the_day_rule_narrows_the_date_window_rather_than_replacing_it():
    """Both have to hold: inside the dates *and* on a matching day."""
    s = _sched(start_month=12, start_day=20, end_month=12, end_day=31,
               weekday_mask=holidays.WEEKDAYS)
    assert matches_today(s, FRI_25_DEC)         # in the window, a Friday
    assert not matches_today(s, MON_31_AUG)     # a Monday, outside the window
    assert not matches_today(s, date(2026, 12, 26))  # in the window, a Saturday


# ------------------------------------------------------------ the tie-break


def test_a_narrower_day_rule_wins_a_priority_tie():
    every = _sched(id=1, name="every")
    weekdays = _sched(id=2, name="weekdays", weekday_mask=holidays.WEEKDAYS)
    assert pick_schedule([every, weekdays], MON_31_AUG) is weekdays


def test_a_holidays_mode_schedule_outranks_a_weekday_one():
    weekdays = _sched(id=1, name="weekdays", weekday_mask=holidays.WEEKDAYS)
    xmas = _sched(id=2, name="xmas", date_mode=holidays.DATE_HOLIDAYS,
                  holiday_keys=("christmas",))
    assert pick_schedule([weekdays, xmas], FRI_25_DEC) is xmas


def test_always_loses_a_tie_to_a_real_range():
    """`always` is the widest window there is, so it is the least specific."""
    everywhere = _sched(id=1, name="always", date_mode=holidays.DATE_ALWAYS)
    december = _sched(id=2, name="dec", date_mode=holidays.DATE_RANGE,
                      start_month=12, start_day=1, end_month=12, end_day=31)
    assert pick_schedule([everywhere, december], FRI_25_DEC) is december


def test_always_matches_any_date_at_all():
    s = _sched(date_mode=holidays.DATE_ALWAYS,
               start_month=12, start_day=25, end_month=12, end_day=25)
    assert matches_today(s, MON_31_AUG)
    assert matches_today(s, FRI_25_DEC)


def test_always_still_honours_the_weekday_rule():
    """Unlike holidays mode, `always` leaves the Days control live."""
    s = _sched(date_mode=holidays.DATE_ALWAYS, weekday_mask=holidays.WEEKDAYS)
    assert matches_today(s, MON_31_AUG)
    assert not matches_today(s, SUN_30_AUG)


def test_priority_still_outranks_specificity():
    every = _sched(id=1, name="every", priority=200)
    weekdays = _sched(id=2, name="weekdays", weekday_mask=holidays.WEEKDAYS)
    assert pick_schedule([every, weekdays], MON_31_AUG) is every


def test_a_time_window_still_outranks_the_day_rule():
    """Order is priority, then time, then days — a finer grain wins first."""
    evening = _sched(id=1, name="evening", start_minute=18 * 60, end_minute=22 * 60)
    weekdays = _sched(id=2, name="weekdays", weekday_mask=holidays.WEEKDAYS)
    at_8pm = datetime(2026, 8, 31, 20, 0)
    assert pick_schedule([evening, weekdays], at_8pm) is evening


# -------------------------------------------------------------- next change


def test_the_next_change_is_found_at_a_day_boundary():
    """A day rule can only change at midnight, which is always a candidate."""
    default = Mp3File(id=99, label="default", filename="d.mp3", size_bytes=1)
    weekend = _sched(id=1, name="weekend", weekday_mask=holidays.WEEKEND)

    # Friday afternoon: nothing matches, so the default is playing.
    friday = datetime(2026, 8, 28, 15, 0)
    assert friday.date().weekday() == holidays.FRIDAY

    found = find_next_change([weekend], default, friday)
    assert found is not None
    when, resolution = found
    assert when == datetime(2026, 8, 29, 0, 0)
    assert resolution.schedule is weekend


# ------------------------------------------------------------- in words


@pytest.mark.parametrize(("kwargs", "expected"), [
    ({}, ""),
    ({"weekday_mask": holidays.WEEKDAYS}, "Mo–Fr"),
    ({"weekday_mask": holidays.WEEKEND}, "Sa–Su"),
    ({"weekday_mask": holidays.WEEKDAYS, "skip_public_holidays": True},
     "Mo–Fr, not on public holidays"),
    # A range never consults its holiday ticks, so they say nothing here.
    ({"holiday_keys": ("christmas",)}, ""),
    ({"skip_public_holidays": True, "holiday_keys": ("christmas",)},
     "Every day, not on public holidays"),
    ({"weekday_mask": holidays.WEEKDAYS, "holiday_keys": ("christmas", "sinterklaas")},
     "Mo–Fr"),
    # In holidays mode the days *are* the holidays, so they are the sentence
    # -- and the stored weekday mask is not mentioned, because nothing reads it.
    ({"date_mode": holidays.DATE_HOLIDAYS, "weekday_mask": holidays.WEEKDAYS,
      "holiday_keys": ("christmas", "sinterklaas")},
     "Christmas Day, Sinterklaas"),
    ({"date_mode": holidays.DATE_ALWAYS, "weekday_mask": holidays.WEEKDAYS}, "Mo–Fr"),
])
def test_the_rule_reads_as_a_sentence(kwargs, expected):
    assert describe_days(_sched(**kwargs)) == expected


# ------------------------------------------------------ what the row shows


def test_all_days_is_the_absence_of_a_rule_not_a_stored_flag():
    assert _sched().is_every_weekday
    assert not _sched(weekday_mask=holidays.WEEKDAYS).is_every_weekday
    assert not _sched(skip_public_holidays=True).is_every_weekday


def test_holidays_do_not_make_the_weekday_rule_custom():
    """They live in the Dates column and say nothing about weekdays.

    A schedule that runs every weekday and additionally on Christmas still has
    the default weekday rule, so the Days column must sit on All.
    """
    assert _sched(holiday_keys=("christmas",)).is_every_weekday


def test_an_unflushed_schedule_reads_as_all_days():
    """`None` is what the mask is before the INSERT applies its default."""
    assert _sched(weekday_mask=None).is_every_weekday


@pytest.mark.parametrize(("kwargs", "expected"), [
    ({}, "Every day"),
    ({"weekday_mask": holidays.WEEKDAYS}, "Mo–Fr"),
    ({"weekday_mask": holidays.WEEKEND}, "Sa–Su"),
    ({"weekday_mask": 0b0010101}, "Mo, We, Fr"),
    ({"weekday_mask": holidays.WEEKDAYS, "skip_public_holidays": True},
     "Mo–Fr · skipping"),
    # The holidays belong to the Dates column, so they never appear here --
    # two controls describing the same fact is two controls that can disagree.
    ({"weekday_mask": holidays.WEEKDAYS, "holiday_keys": ("christmas",)},
     "Mo–Fr"),
    ({"weekday_mask": holidays.WEEKDAYS, "holiday_keys": ("christmas", "sinterklaas"),
      "skip_public_holidays": True}, "Mo–Fr · skipping"),
    ({"weekday_mask": holidays.NO_DAYS, "holiday_keys": ("christmas",)},
     "No weekday"),
    # Skip has nothing to act on without a weekday, so it is not mentioned.
    ({"weekday_mask": holidays.NO_DAYS, "holiday_keys": ("christmas",),
      "skip_public_holidays": True}, "No weekday"),
])
def test_the_row_summary_says_what_the_rule_is(kwargs, expected):
    assert _sched(**kwargs).day_summary == expected


@pytest.mark.parametrize(("keys", "expected"), [
    ((), "No holidays"),
    (("christmas",), "Christmas Day"),
    # Both fit the column, so both are named rather than counted.
    (("christmas", "sinterklaas"), "Christmas Day, Sinterklaas"),
])
def test_the_dates_cell_summarises_the_holidays(keys, expected):
    assert _sched(holiday_keys=keys).holiday_summary == expected


def test_a_long_selection_names_what_fits_and_counts_the_rest():
    """'5 holidays' names nothing you could act on; the first few do."""
    keys = ("new_year", "easter_monday", "labour_day", "ascension", "whit_monday")
    summary = _sched(holiday_keys=keys).holiday_summary
    assert summary.endswith(" others")
    assert summary.startswith("New Year's Day")
    assert len(summary) <= holidays.NAME_LIST_BUDGET + len(" and 4 others")


def test_a_single_holiday_is_named_rather_than_counted():
    """'1 holiday' tells you nothing; the name tells you everything."""
    assert _sched(holiday_keys=("christmas",)).holiday_summary == "Christmas Day"


def test_the_holiday_summary_never_mentions_the_skip():
    """That switch lives in the Days column and is reported by `day_summary`."""
    summary = _sched(weekday_mask=holidays.WEEKDAYS,
                     holiday_keys=("christmas",),
                     skip_public_holidays=True).holiday_summary
    assert summary == "Christmas Day"
