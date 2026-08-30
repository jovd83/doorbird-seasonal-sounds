"""Keeping a century of moveable holiday dates on disk.

Five of the nineteen holidays move with Easter. `app.holidays` can compute any
of them from any year, and the resolver does exactly that -- but "when is
Easter Monday in 2071" is a question the reference page has to answer for a
hundred rows at a time, and computing a century of them on every page render
to throw the result away is silly work.

So they are written down once: five holidays times a hundred years, 500 rows.
The store is a cache with a guarantee attached -- *at least* a century ahead of
today is always present -- and `ensure_horizon` is what maintains it. It runs
at every boot and inserts only what is missing, so the first start after an
upgrade fills the table and every start after that is a no-op costing one
SELECT.

Nothing is ever deleted. Rows for years gone by are five per year and answer
"what did this look like last Easter" for free.
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from app import holidays
from app.models import HolidayDate
from app.timezone import now_local

log = logging.getLogger("doorbird.holidays")


def ensure_horizon(db: Session, today: date | None = None) -> int:
    """Store any missing moveable dates out to the horizon. Returns how many.

    Idempotent, and cheap to call when there is nothing to do: the whole
    horizon is one indexed read and the common case inserts nothing.
    """
    day = today or now_local().date()
    first_year = day.year

    wanted = holidays.moving_dates(first_year, holidays.HORIZON_YEARS)
    have = {
        (row.holiday_key, row.on_date)
        for row in db.query(HolidayDate)
        .filter(HolidayDate.on_date >= date(first_year, 1, 1))
        .all()
    }
    missing = [pair for pair in wanted if pair not in have]
    if not missing:
        return 0

    db.add_all(HolidayDate(holiday_key=key, on_date=on) for key, on in missing)
    db.flush()
    log.info(
        "stored %d moveable holiday dates (%d–%d)",
        len(missing), first_year, first_year + holidays.HORIZON_YEARS - 1,
    )
    return len(missing)


def stored_span(db: Session) -> tuple[date, date] | None:
    """Earliest and latest stored date, for the reference page's footer."""
    first = db.query(HolidayDate).order_by(HolidayDate.on_date).first()
    last = db.query(HolidayDate).order_by(HolidayDate.on_date.desc()).first()
    if first is None or last is None:
        return None
    return first.on_date, last.on_date


def next_dates(db: Session, on_or_after: date | None = None) -> dict[str, date]:
    """The next occurrence of every holiday, keyed by holiday.

    Fixed dates are worked out arithmetically -- there is nothing stored for
    them and nothing to look up. The moveable ones are read from the table,
    which is the whole point of having it, and fall back to computing when a
    date somehow is not there (an empty table on the very first render, before
    `ensure_horizon` has run).
    """
    day = on_or_after or now_local().date()
    found: dict[str, date] = {}

    rows = (
        db.query(HolidayDate)
        .filter(HolidayDate.on_date >= day)
        .order_by(HolidayDate.on_date)
        .all()
    )
    for row in rows:
        found.setdefault(row.holiday_key, row.on_date)

    for holiday in holidays.HOLIDAYS:
        if holiday.key not in found:
            found[holiday.key] = holidays.next_occurrence(holiday.key, day)
    return found
