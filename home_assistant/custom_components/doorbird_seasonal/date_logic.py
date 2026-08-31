"""Date and time-of-day window matching for seasonal schedules.

**This is a vendored copy of the rules in the Docker app's `app/date_logic.py`.**
A Home Assistant custom component is deployed on its own into HA's config
directory and cannot import from the app, so the rules have to live here too.
What it must not do is *disagree*, and it used to: this file had no concept of a
time-of-day window at all, so a schedule with `start_time`/`end_time` resolved
one way in the app and another way on the HA sensor, with nothing to say which
was right. `tests/test_ha_date_logic.py` now runs both implementations over the
same fixtures and fails if their answers diverge.

`holidays.py` beside this file is a *byte-identical* copy of the app's
`app/holidays.py` -- the catalogue and the Easter arithmetic travel with these
rules, because a weekday mask is no use without knowing which day is a holiday.
A test asserts the two files are the same bytes, so update it by copying, never
by editing this one.

Keep the two in step. The tie-break order in particular is load-bearing:
highest priority, then the narrowest time window, then the fewest weekdays,
then the narrowest date range, then a stable key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

try:  # inside Home Assistant this file is one module of a package
    from . import holidays
except ImportError:  # loaded on its own by the agreement test
    import holidays

MINUTES_PER_DAY = 24 * 60


@dataclass
class Schedule:
    name: str
    mp3: str
    start_month: int
    start_day: int
    end_month: int | None = None
    end_day: int | None = None
    start_year: int | None = None
    end_year: int | None = None
    priority: int = 100
    # Minutes since midnight. Both None means the schedule runs all day; a
    # start later than the end wraps past midnight (22:00 -> 02:00).
    start_minute: int | None = None
    end_minute: int | None = None
    enabled: bool = True
    # Which weekdays the schedule may fire on; bit 0 is Monday. 127 is every
    # day, which is what a schedule with no day rule of its own means.
    weekday_mask: int = holidays.ALL_DAYS
    skip_public_holidays: bool = False
    holiday_keys: frozenset[str] = field(default_factory=frozenset)
    # Which of the three exclusive date rules applies. Defaults to `range`,
    # which is what every schedule written before the column meant.
    date_mode: str = holidays.DATE_RANGE

    @property
    def all_day(self) -> bool:
        return self.start_minute is None and self.end_minute is None


@dataclass
class Resolution:
    schedule: Schedule | None
    mp3: str
    reason: str


def _end_md(s: Schedule) -> tuple[int, int]:
    return (s.end_month or s.start_month, s.end_day or s.start_day)


def matches_date(s: Schedule, today: date) -> bool:
    """Does the schedule cover this date at all?

    Three mutually exclusive modes, which is the point of the column:

    * `always`    -- no calendar restriction; every date qualifies.
    * `holidays`  -- only the named holidays, whatever date they land on.
    * `range`     -- the stored month/day window, as it has always worked.

    Holidays used to be additive *inside* a mandatory window, so "only on
    Christmas" could not be said without also inventing a range that happened
    to contain it. Making the three exclusive is what makes that sayable.
    """
    if not s.enabled:
        return False

    mode = holidays.effective_date_mode(s.date_mode)
    if mode == holidays.DATE_ALWAYS:
        return True
    if mode == holidays.DATE_HOLIDAYS:
        return bool((s.holiday_keys or frozenset()) & holidays.keys_on(today))

    end_m, end_d = _end_md(s)

    if s.start_year is not None:
        # One-off, possibly spanning multiple calendar years.
        try:
            sd = date(s.start_year, s.start_month, s.start_day)
            ed = date(s.end_year or s.start_year, end_m, end_d)
        except ValueError:
            return False
        if ed < sd:
            return False
        return sd <= today <= ed

    # Recurring annually — only month/day matters; year-end wrap is supported.
    start_key = (s.start_month, s.start_day)
    end_key = (end_m, end_d)
    today_key = (today.month, today.day)

    if start_key <= end_key:
        return start_key <= today_key <= end_key
    return today_key >= start_key or today_key <= end_key


def matches_time(s: Schedule, minute_of_day: int) -> bool:
    """Does the schedule's time-of-day window cover this minute?

    All-day schedules (both bounds None) always match. A window whose start is
    later than its end wraps past midnight, so 22:00–02:00 covers late evening
    and the small hours. Both bounds are inclusive of their minute.
    """
    start, end = s.start_minute, s.end_minute
    if start is None and end is None:
        return True
    start = 0 if start is None else start
    end = MINUTES_PER_DAY - 1 if end is None else end

    if start <= end:
        return start <= minute_of_day <= end
    return minute_of_day >= start or minute_of_day <= end


def matches_day(s: Schedule, today: date) -> bool:
    """Does the schedule's weekday rule cover this day?

    In `holidays` mode there is nothing left to narrow: the person named the
    exact days they wanted, so a weekday rule could only take one of them
    away again. Christmas fires on a Christmas that falls on a Sunday even
    when the weekday rule says Mo–Fr. Every other mode applies the mask.

    `skip_public_holidays` is the one subtraction, and it only ever removes a
    day that matched by weekday -- which is why it is inert in `holidays` mode
    too, where nothing matched by weekday in the first place.
    """
    if holidays.effective_date_mode(s.date_mode) == holidays.DATE_HOLIDAYS:
        return True
    if not holidays.day_selected(s.weekday_mask, today.weekday()):
        return False
    skipped = s.skip_public_holidays and bool(
        holidays.keys_on(today) & holidays.PUBLIC_KEYS)
    return not skipped


def matches_today(s: Schedule, today: date) -> bool:
    """Date-only match, kept for callers that only care about the day."""
    return matches_date(s, today) and matches_day(s, today)


def matches_now(s: Schedule, when: datetime) -> bool:
    day = when.date()
    return (matches_date(s, day) and matches_day(s, day)
            and matches_time(s, when.hour * 60 + when.minute))


def _window_span_days(s: Schedule) -> int:
    mode = holidays.effective_date_mode(s.date_mode)
    if mode == holidays.DATE_ALWAYS:
        # The widest window there is, so it loses every tie to a real range.
        return 366
    if mode == holidays.DATE_HOLIDAYS:
        # A handful of named days is the narrowest statement on the page.
        return len(s.holiday_keys or ())

    end_m, end_d = _end_md(s)
    if s.start_year is not None:
        try:
            sd = date(s.start_year, s.start_month, s.start_day)
            ed = date(s.end_year or s.start_year, end_m, end_d)
        except ValueError:
            return 0
        return max(0, (ed - sd).days)

    start_ord = s.start_month * 31 + s.start_day
    end_ord = end_m * 31 + end_d
    if end_ord >= start_ord:
        return end_ord - start_ord
    return (12 * 31) - start_ord + end_ord


def _day_span(s: Schedule) -> int:
    """How many weekdays the rule covers; fewer is more specific.

    A holidays-mode schedule scores 0 and therefore beats every weekday rule
    at the same priority, which is what you want: naming Christmas Day is a
    more deliberate statement than ticking Monday. Its stored mask is ignored
    in that mode, so scoring it would rank on a number nothing reads.
    """
    if holidays.effective_date_mode(s.date_mode) == holidays.DATE_HOLIDAYS:
        return 0
    return holidays.day_count(s.weekday_mask)


def _window_span_minutes(s: Schedule) -> int:
    """How much of the day the schedule covers; narrower wins a priority tie."""
    if s.start_minute is None and s.end_minute is None:
        return MINUTES_PER_DAY
    start = 0 if s.start_minute is None else s.start_minute
    end = MINUTES_PER_DAY - 1 if s.end_minute is None else s.end_minute
    if start <= end:
        return end - start
    return MINUTES_PER_DAY - start + end


def _fmt_minute(value: int | None) -> str:
    if value is None:
        return "--:--"
    return f"{value // 60:02d}:{value % 60:02d}"


def describe_days(schedule: Schedule) -> str:
    """'Mo–Fr, not on public holidays, plus Christmas Day' — the day rule in words.

    Empty for a schedule that runs every day with no holiday rule, so the
    common case adds nothing to a reason string.
    """
    mask = holidays.effective_mask(schedule.weekday_mask)
    picked = schedule.holiday_keys or frozenset()
    named = holidays.names(picked)

    # No weekday at all: the holidays *are* the rule, so name them and stop.
    if mask == holidays.NO_DAYS and named:
        return ", ".join(named)

    subtracts = bool(schedule.skip_public_holidays and mask)
    tail: list[str] = []
    if subtracts:
        tail.append("not on public holidays")
    # With every day ticked and nothing subtracted, a ticked holiday changes
    # nothing -- saying "plus Christmas Day" would imply otherwise.
    if named and (mask != holidays.ALL_DAYS or subtracts):
        tail.append("plus " + ", ".join(named))

    parts: list[str] = []
    if mask != holidays.ALL_DAYS or tail:
        parts.append(holidays.describe_days(mask))
    parts.extend(tail)
    return ", ".join(parts)


def describe(schedule: Schedule) -> str:
    detail = f"schedule '{schedule.name}' (priority {schedule.priority})"
    if not schedule.all_day:
        detail += f" {_fmt_minute(schedule.start_minute)}–{_fmt_minute(schedule.end_minute)}"
    days = describe_days(schedule)
    if days:
        detail += f" [{days}]"
    return detail


def pick_schedule(schedules: list[Schedule], when: date | datetime) -> Schedule | None:
    """The single schedule that wins at this moment, or None if none match.

    Ties break by highest priority, then the most specific window — narrowest
    time range first, then fewest weekdays, then narrowest date range — and
    finally by name so the result is stable. The app orders that last key by
    row id; name is the closest stable equivalent here, and the agreement test
    only uses fixtures where the earlier keys already decide.
    """
    moment = when if isinstance(when, datetime) else datetime.combine(when, datetime.min.time())
    day = moment.date()
    minute = moment.hour * 60 + moment.minute

    candidates = [
        s for s in schedules
        if matches_date(s, day) and matches_day(s, day) and matches_time(s, minute)
    ]
    if not candidates:
        return None

    candidates.sort(
        key=lambda s: (-s.priority, _window_span_minutes(s), _day_span(s),
                       _window_span_days(s), s.name)
    )
    return candidates[0]


def resolve_active(
    schedules: list[Schedule], default_mp3: str, when: date | datetime
) -> Resolution:
    """Pick the sound for a given moment, falling back to the default."""
    winner = pick_schedule(schedules, when)
    if winner is None:
        return Resolution(schedule=None, mp3=default_mp3,
                          reason="no schedule active; using default")
    return Resolution(schedule=winner, mp3=winner.mp3, reason=describe(winner))
