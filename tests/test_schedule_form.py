"""The schedule form helpers: date parsing + recurring/year handling.

These import from `app.schedule_form`, not from the router. The rules used to
live inside `routes/schedules.py` and raise `HTTPException`, so exercising them
meant importing a FastAPI module to ask whether "25:00" is a valid time.
"""

import pytest

from app import holidays
from app.schedule_form import FormError, build_date_fields, build_day_rule


def test_recurring_strips_years():
    f = build_date_fields(start="2026-12-20", end="2027-01-06", recurring=True)
    assert f == {
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
    rule = build_day_rule(weekdays=["0", "2", "4"], holiday_keys=[],
                          skip_public_holidays=False)
    assert rule.weekday_mask == 0b0010101
    assert rule.holiday_keys == ()
    assert rule.as_fields() == {"weekday_mask": 0b0010101, "skip_public_holidays": False}


def test_the_presets_round_trip_to_the_masks_they_name():
    weekdays = build_day_rule(weekdays=["0", "1", "2", "3", "4"], holiday_keys=[],
                              skip_public_holidays=False)
    assert weekdays.weekday_mask == holidays.WEEKDAYS
    weekend = build_day_rule(weekdays=["5", "6"], holiday_keys=[],
                             skip_public_holidays=False)
    assert weekend.weekday_mask == holidays.WEEKEND


@pytest.mark.parametrize("bad", ["7", "-1", "12"])
def test_a_day_outside_the_week_is_refused(bad):
    with pytest.raises(FormError, match="day of the week"):
        build_day_rule(weekdays=[bad], holiday_keys=[], skip_public_holidays=False)


def test_a_day_that_is_not_a_number_is_refused():
    with pytest.raises(FormError, match="not a day of the week"):
        build_day_rule(weekdays=["monday"], holiday_keys=[], skip_public_holidays=False)


def test_an_unknown_holiday_is_refused_rather_than_ignored():
    with pytest.raises(FormError, match="unknown holiday"):
        build_day_rule(weekdays=["0"], holiday_keys=["thanksgiving"],
                       skip_public_holidays=False)


def test_duplicate_holiday_keys_collapse():
    rule = build_day_rule(weekdays=["0"], holiday_keys=["christmas", "christmas"],
                          skip_public_holidays=False)
    assert rule.holiday_keys == ("christmas",)


def test_a_rule_that_could_never_fire_is_refused():
    with pytest.raises(FormError, match="at least one day"):
        build_day_rule(weekdays=[], holiday_keys=[], skip_public_holidays=False)


def test_holidays_alone_are_a_complete_rule():
    rule = build_day_rule(weekdays=[], holiday_keys=["christmas"],
                          skip_public_holidays=False)
    assert rule.weekday_mask == holidays.NO_DAYS
    assert rule.holiday_keys == ("christmas",)


def test_skip_is_dropped_when_there_is_no_weekday_to_subtract_from():
    """The form disables the switch in this state; a round trip must agree."""
    rule = build_day_rule(weekdays=[], holiday_keys=["christmas"],
                          skip_public_holidays=True)
    assert rule.skip_public_holidays is False


def test_a_post_without_the_day_controls_means_every_day():
    """A client that predates the feature must keep behaving as it did.

    Unchecked boxes submit nothing, so "no days ticked" and "no day fields at
    all" arrive identical on the wire. The form's hidden marker is what tells
    them apart, and without it the answer is the old one.
    """
    rule = build_day_rule(weekdays=[], holiday_keys=[], skip_public_holidays=False,
                          present=False)
    assert rule.weekday_mask == holidays.ALL_DAYS
    assert rule.holiday_keys == ()
    assert rule.skip_public_holidays is False
