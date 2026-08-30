"""The holiday catalogue, the Easter arithmetic, and the stored century.

Easter is the whole reason this module exists, so it is checked against dates
that were looked up rather than computed — including years well outside the
range the app will ever store, because an off-by-one in the algorithm shows up
at the edges of the century rules, not in the middle.
"""
from datetime import date, timedelta

import pytest

from app import holidays

# Gregorian Easter Sundays, from published tables rather than from the code
# under test. Spread deliberately: a March Easter, an April one, a leap year,
# a century year, and both ends of the horizon the app stores.
KNOWN_EASTER = {
    1900: date(1900, 4, 15),
    2000: date(2000, 4, 23),
    2001: date(2001, 4, 15),
    2024: date(2024, 3, 31),
    2025: date(2025, 4, 20),
    2026: date(2026, 4, 5),
    2027: date(2027, 3, 28),
    2028: date(2028, 4, 16),
    2030: date(2030, 4, 21),
    2038: date(2038, 4, 25),   # the latest Easter can ever fall
    2085: date(2085, 4, 15),
    2125: date(2125, 4, 22),
}


@pytest.mark.parametrize("year", sorted(KNOWN_EASTER))
def test_easter_matches_published_dates(year):
    assert holidays.easter(year) == KNOWN_EASTER[year]


def test_easter_is_always_a_sunday():
    """A cheap invariant that would catch an offset error in any year."""
    for year in range(1900, 2200):
        assert holidays.easter(year).weekday() == holidays.SUNDAY


def test_easter_stays_inside_its_calendar_window():
    """Easter can only fall between 22 March and 25 April, ever."""
    for year in range(1900, 2200):
        day = holidays.easter(year)
        assert date(year, 3, 22) <= day <= date(year, 4, 25)


# --------------------------------------------------------------- catalogue


def test_the_catalogue_is_what_was_asked_for():
    assert len(holidays.HOLIDAYS) == 19
    assert len(holidays.PUBLIC_KEYS) == 10
    assert len([h for h in holidays.HOLIDAYS
                if h.group == holidays.GROUP_COMMUNITY]) == 3
    assert len([h for h in holidays.HOLIDAYS
                if h.group == holidays.GROUP_OBSERVANCE]) == 6


def test_keys_are_unique_and_every_entry_has_exactly_one_rule():
    keys = [h.key for h in holidays.HOLIDAYS]
    assert len(keys) == len(set(keys))
    for h in holidays.HOLIDAYS:
        fixed = h.month is not None and h.day is not None
        assert fixed != h.moves, f"{h.key} must be fixed or moveable, not both"


def test_every_group_member_is_reachable_through_grouped():
    listed = [h.key for _, _, members in holidays.grouped() for h in members]
    assert sorted(listed) == sorted(h.key for h in holidays.HOLIDAYS)


def test_the_five_moveable_ones_are_the_easter_family():
    easter_family = {
        "easter_sunday", "easter_monday", "ascension", "whit_sunday", "whit_monday"}
    assert sorted(holidays.MOVING_KEYS) == sorted(easter_family)


@pytest.mark.parametrize(("key", "offset"), [
    ("easter_sunday", 0),
    ("easter_monday", 1),
    ("ascension", 39),
    ("whit_sunday", 49),
    ("whit_monday", 50),
])
def test_moveable_offsets_land_where_the_church_calendar_says(key, offset):
    """Ascension is 40 days *inclusive* of Easter, hence 39 days after it."""
    for year in (2026, 2027, 2030):
        assert holidays.occurrences(year)[key] == holidays.easter(year) + timedelta(offset)


def test_ascension_is_always_a_thursday_and_whit_monday_a_monday():
    for year in range(2026, 2126):
        dated = holidays.occurrences(year)
        assert dated["ascension"].weekday() == holidays.THURSDAY
        assert dated["whit_monday"].weekday() == holidays.MONDAY


# ------------------------------------------------------------------ lookups


def test_keys_on_finds_fixed_and_moveable_alike():
    assert "christmas" in holidays.keys_on(date(2026, 12, 25))
    assert "easter_monday" in holidays.keys_on(date(2027, 3, 29))
    assert holidays.keys_on(date(2026, 8, 30)) == frozenset()


def test_a_lookup_returns_every_holiday_on_that_day_not_just_the_first():
    """`keys_on` is a set for a reason: two entries can land on one date.

    German-speaking Community Day is 15 November, and Whit Sunday is an
    observance that moves — in 2038 it falls on 13 June, the same day nothing
    else does, but the shape has to be a set either way because the catalogue
    does not guarantee one holiday per date.
    """
    assert holidays.keys_on(date(2026, 11, 15)) == {"german_community"}
    assert holidays.keys_on(date(2027, 3, 28)) == {"easter_sunday"}


def test_only_the_federal_ten_count_as_public():
    assert holidays.is_public_holiday(date(2026, 12, 25))      # Christmas
    assert holidays.is_public_holiday(date(2026, 11, 11))      # Armistice
    # A community day and an observance are holidays, but not public ones.
    assert not holidays.is_public_holiday(date(2026, 12, 6))   # Sinterklaas
    assert not holidays.is_public_holiday(date(2026, 11, 15))  # German-speaking


def test_next_occurrence_counts_today_and_rolls_into_next_year():
    assert holidays.next_occurrence("christmas", date(2026, 12, 25)) == date(2026, 12, 25)
    assert holidays.next_occurrence("christmas", date(2026, 12, 26)) == date(2027, 12, 25)
    assert holidays.next_occurrence("easter_monday", date(2026, 8, 30)) == date(2027, 3, 29)


# -------------------------------------------------------------- the bitmask


def test_presets_are_the_masks_they_claim_to_be():
    assert holidays.describe_days(holidays.ALL_DAYS) == "Every day"
    assert holidays.describe_days(holidays.WEEKDAYS) == "Mo–Fr"
    assert holidays.describe_days(holidays.WEEKEND) == "Sa–Su"
    assert holidays.describe_days(0b0010101) == "Mo, We, Fr"


def test_an_unset_mask_reads_as_every_day():
    """`None` is what an unflushed row and a pre-migration row both carry."""
    assert holidays.effective_mask(None) == holidays.ALL_DAYS
    assert holidays.day_count(None) == 7
    assert all(holidays.day_selected(None, i) for i in range(7))


def test_weekday_bits_line_up_with_date_weekday():
    """Bit 0 is Monday, which is what `date.weekday()` returns for Monday."""
    monday = date(2026, 8, 31)
    assert monday.weekday() == holidays.MONDAY
    assert holidays.day_selected(holidays.WEEKDAYS, monday.weekday())
    sunday = date(2026, 8, 30)
    assert sunday.weekday() == holidays.SUNDAY
    assert not holidays.day_selected(holidays.WEEKDAYS, sunday.weekday())
    assert holidays.day_selected(holidays.WEEKEND, sunday.weekday())


def test_names_come_back_in_catalogue_order_not_selection_order():
    assert holidays.names({"sinterklaas", "new_year"}) == ["New Year's Day", "Sinterklaas"]


# --------------------------------------------------------------- the store


def test_moving_dates_covers_a_century_of_every_moveable_holiday():
    rows = holidays.moving_dates(2026, holidays.HORIZON_YEARS)
    assert len(rows) == len(holidays.MOVING) * holidays.HORIZON_YEARS
    # Nothing spills into a neighbouring year: the latest of the five is Whit
    # Monday, which is mid-June at worst.
    assert {on.year for _, on in rows} == set(range(2026, 2126))


def test_ensure_horizon_fills_then_does_nothing(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.holiday_store import ensure_horizon, next_dates, stored_span
    from app.models import HolidayDate

    engine = create_engine(f"sqlite:///{tmp_path / 'h.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    written = ensure_horizon(session, date(2026, 8, 30))
    session.commit()
    assert written == len(holidays.MOVING) * holidays.HORIZON_YEARS
    assert session.query(HolidayDate).count() == written

    # Idempotent: a second boot has nothing left to do.
    assert ensure_horizon(session, date(2026, 8, 30)) == 0

    first, last = stored_span(session)
    assert first.year == 2026
    assert last == holidays.occurrences(2125)["whit_monday"]

    # A year on, the horizon has rolled and only the new year is inserted.
    assert ensure_horizon(session, date(2027, 1, 1)) == len(holidays.MOVING)
    session.commit()

    upcoming = next_dates(session, date(2026, 8, 30))
    assert upcoming["easter_monday"] == date(2027, 3, 29)
    assert upcoming["christmas"] == date(2026, 12, 25)
    session.close()


def test_the_stored_table_agrees_with_the_computation(tmp_path):
    """The table is a cache. A cache that disagrees with its source is a bug."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.holiday_store import ensure_horizon
    from app.models import HolidayDate

    engine = create_engine(f"sqlite:///{tmp_path / 'agree.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    ensure_horizon(session, date(2026, 8, 30))
    session.commit()

    stored = {(r.holiday_key, r.on_date) for r in session.query(HolidayDate).all()}
    computed = set(holidays.moving_dates(2026, holidays.HORIZON_YEARS))
    assert stored == computed
    session.close()
