"""Parsing and validating the schedule form, with no web framework in sight.

These rules used to live in `routes/schedules.py` and raise `HTTPException`
directly, which meant the only way to ask "is 25:00 rejected?" was to make an
HTTP request. They raise `FormError` now; the router turns that into a 400.
The behaviour is identical from the outside and the rules are unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app import holidays

MINUTES_PER_DAY = 24 * 60

# The two settings of the day-rule radio. `all` is the default a schedule
# has always had; `custom` is what opens the day/holiday editor.
MODE_ALL = "all"
MODE_CUSTOM = "custom"

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


@dataclass(frozen=True)
class DayRule:
    """The weekday/holiday half of the form, parsed and checked."""

    weekday_mask: int
    holiday_keys: tuple[str, ...]
    skip_public_holidays: bool

    def as_fields(self) -> dict[str, int | bool]:
        """Only the plain columns; the keys go through `set_holiday_keys`."""
        return {
            "weekday_mask": self.weekday_mask,
            "skip_public_holidays": self.skip_public_holidays,
        }


def build_day_rule(
    *,
    mode: str,
    weekdays: list[str] | None,
    holiday_keys: list[str] | None,
    skip_public_holidays: bool,
) -> DayRule:
    """Turn the day-mode radio and the modal's fields into stored fields.

    `mode` is the row's "All / Custom" choice and doubles as the marker that
    this submission carried the day controls at all. Anything other than
    `custom` -- including a missing field, which is what a client written
    before this feature sends -- means every day with no holiday rule, and
    that is what such a schedule has always done.

    On `custom` the seven day numbers and the holiday keys are read as posted.
    The form sends weekday numbers rather than a mask because seven checkboxes
    named the same thing is what HTML gives you, and reassembling them here
    beats asking the browser for arithmetic.

    A custom rule that ticks no day and no holiday can never fire. That is
    almost always a half-finished edit rather than an intent to silence the
    schedule -- there is an Enabled switch for that -- so it is rejected rather
    than saved as something that quietly never plays.
    """
    if (mode or "").strip().lower() != MODE_CUSTOM:
        return DayRule(weekday_mask=holidays.ALL_DAYS, holiday_keys=(),
                       skip_public_holidays=False)

    mask = 0
    for raw in weekdays or []:
        text = str(raw).strip()
        if not text:
            continue
        try:
            index = int(text)
        except ValueError as exc:
            raise FormError(f"{text!r} is not a day of the week") from exc
        if not 0 <= index <= 6:
            raise FormError(f"day of the week must be 0-6, got {index}")
        mask |= 1 << index

    keys: list[str] = []
    for raw in holiday_keys or []:
        key = str(raw).strip()
        if not key:
            continue
        if key not in holidays.VALID_KEYS:
            raise FormError(f"unknown holiday {key!r}")
        if key not in keys:
            keys.append(key)

    if not mask and not keys:
        raise FormError(
            "pick at least one day of the week, or one holiday - "
            "a schedule with neither could never play")

    # With no weekday ticked there is nothing for the skip to subtract from,
    # and a stored true would be a trap: the modal disables the switch in that
    # state, so it must not survive a round trip either.
    return DayRule(
        weekday_mask=mask,
        holiday_keys=tuple(keys),
        skip_public_holidays=bool(skip_public_holidays) and bool(mask),
    )


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
