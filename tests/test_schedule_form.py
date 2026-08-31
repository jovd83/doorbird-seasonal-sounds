"""The schedule form helpers: date parsing + recurring/year handling.

These import from `app.schedule_form`, not from the router. The rules used to
live inside `routes/schedules.py` and raise `HTTPException`, so exercising them
meant importing a FastAPI module to ask whether "25:00" is a valid time.
"""

import pytest

from app import holidays
from app.schedule_form import (
    DayRule,
    FormError,
    build_date_fields,
    build_day_rule,
    parse_holiday_keys,
)


def test_recurring_strips_years():
    f = build_date_fields(start="2026-12-20", end="2027-01-06", recurring=True)
    assert f == {
        "date_mode": holidays.DATE_RANGE,
        "start_month": 12, "start_day": 20,
        "end_month": 1, "end_day": 6,
        "start_year": None, "end_year": None,
    }


def test_one_off_single_year():
    f = build_date_fields(start="2026-04-03", end="2026-04-06", recurring=False)
    assert f["start_year"] == 2026
    assert f["end_year"] == 2026
    assert (f["start_month"], f["start_day"]) == (4, 3)
    assert (f["end_month"], f["end_day"]) == (4, 6)


def test_one_off_multi_year_wrap_allowed():
    f = build_date_fields(start="2026-12-20", end="2027-01-06", recurring=False)
    assert f["start_year"] == 2026
    assert f["end_year"] == 2027
    assert (f["start_month"], f["start_day"]) == (12, 20)
    assert (f["end_month"], f["end_day"]) == (1, 6)


def test_single_day_one_off_folds_end_fields():
    f = build_date_fields(start="2026-07-14", end="", recurring=False)
    assert f["end_month"] is None
    assert f["end_day"] is None
    assert f["end_year"] is None
    assert f["start_year"] == 2026


def test_single_day_recurring_folds_end_fields():
    f = build_date_fields(start="2026-07-14", end="2026-07-14", recurring=True)
    assert f["end_month"] is None
    assert f["end_day"] is None
    assert f["start_year"] is None


def test_rejects_end_before_start():
    with pytest.raises(FormError, match="end date is before start date"):
        build_date_fields(start="2026-09-10", end="2026-09-01", recurring=False)


def test_rejects_bad_date_format():
    with pytest.raises(FormError, match="YYYY-MM-DD"):
        build_date_fields(start="12-20", end="", recurring=True)


# --------------------------------------------------------------- day rules


def test_weekday_numbers_become_a_bitmask():
    rule = build_day_rule(mode="custom", weekdays=["0", "2", "4"],
                          skip_public_holidays=False)
    assert rule.weekday_mask == 0b0010101
    assert rule.as_fields() == {"weekday_mask": 0b0010101, "skip_public_holidays": False}


def test_the_presets_round_trip_to_the_masks_they_name():
    weekdays = build_day_rule(mode="custom", weekdays=["0", "1", "2", "3", "4"],
                              skip_public_holidays=False)
    assert weekdays.weekday_mask == holidays.WEEKDAYS
    weekend = build_day_rule(mode="custom", weekdays=["5", "6"],
                             skip_public_holidays=False)
    assert weekend.weekday_mask == holidays.WEEKEND


@pytest.mark.parametrize("bad", ["7", "-1", "12"])
def test_a_day_outside_the_week_is_refused(bad):
    with pytest.raises(FormError, match="day of the week"):
        build_day_rule(mode="custom", weekdays=[bad], skip_public_holidays=False)


def test_a_day_that_is_not_a_number_is_refused():
    with pytest.raises(FormError, match="not a day of the week"):
        build_day_rule(mode="custom", weekdays=["monday"], skip_public_holidays=False)


def test_a_weekday_rule_with_no_day_is_refused():
    """It could never fire, and there is an Enabled switch for silence."""
    with pytest.raises(FormError, match="at least one day"):
        build_day_rule(mode="custom", weekdays=[], skip_public_holidays=False)


def test_skip_is_dropped_when_there_is_no_weekday_to_subtract_from():
    """Unreachable through the form, which refuses an empty rule outright."""
    rule = build_day_rule(mode="custom", weekdays=["0"], skip_public_holidays=True)
    assert rule.skip_public_holidays is True
    assert DayRule.unrestricted().skip_public_holidays is False


# ------------------------------------------------------------- date modes


def test_an_unknown_holiday_is_refused_rather_than_ignored():
    with pytest.raises(FormError, match="unknown holiday"):
        parse_holiday_keys(["thanksgiving"])


def test_duplicate_holiday_keys_collapse():
    assert parse_holiday_keys(["christmas", "christmas"]) == ("christmas",)
    assert parse_holiday_keys([]) == ()
    assert parse_holiday_keys(None) == ()


def test_always_stores_a_full_year_and_forgets_the_typed_dates():
    f = build_date_fields(start="2026-12-20", end="2027-01-06", recurring=False,
                          mode=holidays.DATE_ALWAYS)
    assert f["date_mode"] == holidays.DATE_ALWAYS
    assert (f["start_month"], f["start_day"]) == (1, 1)
    assert (f["end_month"], f["end_day"]) == (12, 31)
    assert f["start_year"] is None and f["end_year"] is None


def test_holidays_mode_needs_at_least_one_holiday():
    with pytest.raises(FormError, match="at least one holiday"):
        build_date_fields(start="2026-12-20", end="", recurring=True,
                          mode=holidays.DATE_HOLIDAYS, holiday_keys=())


def test_holidays_mode_stores_the_mode_and_a_full_year():
    f = build_date_fields(start="2026-12-20", end="", recurring=True,
                          mode=holidays.DATE_HOLIDAYS, holiday_keys=("christmas",))
    assert f["date_mode"] == holidays.DATE_HOLIDAYS
    assert (f["start_month"], f["end_month"]) == (1, 12)


def test_an_unrecognised_date_mode_reads_as_a_range():
    """What every schedule written before the column meant."""
    f = build_date_fields(start="2026-12-20", end="2026-12-26", recurring=True,
                          mode="nonsense")
    assert f["date_mode"] == holidays.DATE_RANGE
    assert (f["start_month"], f["start_day"]) == (12, 20)


def test_a_range_still_validates_its_dates():
    with pytest.raises(FormError, match="end date is before start date"):
        build_date_fields(start="2026-12-26", end="2026-12-20", recurring=False,
                          mode=holidays.DATE_RANGE)


@pytest.mark.parametrize("mode", ["all", "", "nonsense", None])
def test_anything_but_custom_means_every_weekday(mode):
    """`all` is the radio's other setting; the rest is defensive.

    A missing field is what a client written before this feature sends, and an
    unrecognised one should not be able to invent a third behaviour. Both land
    on every weekday with nothing skipped.
    """
    rule = build_day_rule(mode=mode, weekdays=["0"], skip_public_holidays=True)
    assert rule.weekday_mask == holidays.ALL_DAYS
    assert rule.skip_public_holidays is False


def test_all_mode_discards_whatever_the_modal_still_had_ticked():
    """The mode decides, not the fields the modal left in the DOM.

    They are still posted, so without this a schedule put back to All would
    keep the narrower rule nobody can see selected any more.
    """
    rule = build_day_rule(mode="all", weekdays=["5", "6"],
                          skip_public_holidays=True)
    assert rule.weekday_mask == holidays.ALL_DAYS
    assert rule.skip_public_holidays is False
