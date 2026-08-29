"""Parsing and validating the schedule form, with no web framework in sight.

These rules used to live in `routes/schedules.py` and raise `HTTPException`
directly, which meant the only way to ask "is 25:00 rejected?" was to make an
HTTP request. They raise `FormError` now; the router turns that into a 400.
The behaviour is identical from the outside and the rules are unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MINUTES_PER_DAY = 24 * 60

# An auto response that waits longer than this is almost certainly a typo --
# the visitor is long gone.
MAX_DELAY_SECONDS = 3600


class FormError(ValueError):
    """Something the person filling in the form can fix.

    Deliberately not an HTTPException: this module knows nothing about HTTP,
    and the message is written for a human rather than a status code.
    """


def parse_hhmm(value: str, field: str) -> int:
    """'HH:MM' -> minutes since midnight."""
    raw = (value or "").strip()
    try:
        hh, _, mm = raw.partition(":")
        hours, minutes = int(hh), int(mm)
    except ValueError as exc:
        raise FormError(
            f"{field} must be in 24-hour HH:MM format, got {raw!r}") from exc
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise FormError(f"{field} must be between 00:00 and 23:59, got {raw!r}")
    return hours * 60 + minutes


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise FormError(f"expected YYYY-MM-DD, got {value!r}") from exc


def optional_int(value, field: str) -> int | None:
    """Form ints that may arrive as an empty string from an unset <select>."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise FormError(f"{field} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class TimeWindow:
    start_minute: int | None
    end_minute: int | None

    def as_fields(self) -> dict[str, int | None]:
        return {"start_minute": self.start_minute, "end_minute": self.end_minute}


def build_time_window(*, all_day: bool, start_time: str, end_time: str) -> TimeWindow:
    """All-day schedules store NULL/NULL; a wrapped window is allowed."""
    if all_day:
        return TimeWindow(None, None)
    if not (start_time or "").strip() or not (end_time or "").strip():
        raise FormError("give both a start and an end time, or tick 'All day'")

    start = parse_hhmm(start_time, "start time")
    end = parse_hhmm(end_time, "end time")
    if start == end:
        raise FormError(
            "start and end time are identical - tick 'All day' for a full day")
    return TimeWindow(start, end)


def build_date_fields(*, start: str, end: str, recurring: bool) -> dict[str, int | None]:
    """Turn the two date inputs into the stored month/day/year columns."""
    sd = parse_iso_date(start)
    ed = parse_iso_date(end) if (end or "").strip() else sd

    if recurring:
        same_day = (ed.month, ed.day) == (sd.month, sd.day)
        return {
            "start_month": sd.month,
            "start_day": sd.day,
            "end_month": None if same_day else ed.month,
            "end_day": None if same_day else ed.day,
            "start_year": None,
            "end_year": None,
        }

    if ed < sd:
        raise FormError("end date is before start date")

    single_day = (ed == sd)
    return {
        "start_month": sd.month,
        "start_day": sd.day,
        "end_month": None if single_day else ed.month,
        "end_day": None if single_day else ed.day,
        "start_year": sd.year,
        "end_year": None if single_day else ed.year,
    }


def clean_delay(kind: str, delay_seconds: int, *, auto_response_kind: str) -> int:
    """Chime schedules have no wait; auto responses have a sane one."""
    if kind != auto_response_kind:
        return 0
    if delay_seconds < 0:
        raise FormError("the wait interval cannot be negative")
    if delay_seconds > MAX_DELAY_SECONDS:
        raise FormError(
            f"the wait interval must be at most {MAX_DELAY_SECONDS} seconds")
    return delay_seconds
