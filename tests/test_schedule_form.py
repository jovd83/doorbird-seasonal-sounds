"""The schedule form helpers: date parsing + recurring/year handling.

These import from `app.schedule_form`, not from the router. The rules used to
live inside `routes/schedules.py` and raise `HTTPException`, so exercising them
meant importing a FastAPI module to ask whether "25:00" is a valid time.
"""

import pytest

from app.schedule_form import FormError, build_date_fields


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
