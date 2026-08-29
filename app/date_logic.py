from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.models import Mp3File, Schedule

MINUTES_PER_DAY = 24 * 60


def _end_md(s: Schedule) -> tuple[int, int]:
    return (s.end_month or s.start_month, s.end_day or s.start_day)


def matches_date(s: Schedule, today: date) -> bool:
    """Does the schedule's calendar window cover this day?"""
    if not s.enabled:
        return False

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

    All-day schedules (both bounds NULL) always match. A window whose start is
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


def matches_today(s: Schedule, today: date) -> bool:
    """Date-only match, kept for callers that only care about the day."""
    return matches_date(s, today)


def matches_now(s: Schedule, when: datetime) -> bool:
    return matches_date(s, when.date()) and matches_time(s, when.hour * 60 + when.minute)


def _window_span_days(s: Schedule) -> int:
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


def _window_span_minutes(s: Schedule) -> int:
    """How much of the day the schedule covers; narrower wins a priority tie."""
    if s.start_minute is None and s.end_minute is None:
        return MINUTES_PER_DAY
    start = 0 if s.start_minute is None else s.start_minute
    end = MINUTES_PER_DAY - 1 if s.end_minute is None else s.end_minute
    if start <= end:
        return end - start
    return MINUTES_PER_DAY - start + end


@dataclass
class Resolution:
    schedule: Schedule | None
    mp3: Mp3File
    reason: str


def pick_schedule(
    schedules: list[Schedule],
    today: date | datetime,
    device_id: int | None = None,
) -> Schedule | None:
    """The single schedule that wins at this moment, or None if none match.

    A schedule qualifies when its calendar window, its time-of-day window and
    its device list all cover the request. Ties are broken by highest priority,
    then by the most specific window — narrowest time range first, then
    narrowest date range — and finally by id so the result is stable.

    The caller decides which kind of schedule to hand in: chime schedules and
    auto-response schedules are resolved separately and never compete.
    """
    when = today if isinstance(today, datetime) else datetime.combine(today, datetime.min.time())
    day = when.date()
    minute = when.hour * 60 + when.minute

    candidates = [
        s for s in schedules
        if matches_date(s, day) and matches_time(s, minute) and s.applies_to(device_id)
    ]
    if not candidates:
        return None

    candidates.sort(
        key=lambda s: (-s.priority, _window_span_minutes(s), _window_span_days(s), s.id)
    )
    return candidates[0]


def describe(schedule: Schedule) -> str:
    detail = f"schedule '{schedule.name}' (priority {schedule.priority})"
    if not schedule.all_day:
        detail += f" {_fmt_minute(schedule.start_minute)}–{_fmt_minute(schedule.end_minute)}"
    return detail


def resolve_active(
    schedules: list[Schedule],
    default_mp3: Mp3File,
    today: date | datetime,
    device_id: int | None = None,
) -> Resolution:
    """Pick the chime for a given moment, falling back to the default MP3."""
    winner = pick_schedule(schedules, today, device_id)
    if winner is None:
        return Resolution(schedule=None, mp3=default_mp3, reason="no schedule active; using default")
    return Resolution(schedule=winner, mp3=winner.mp3, reason=describe(winner))


def _fmt_minute(value: int | None) -> str:
    if value is None:
        return "--:--"
    return f"{value // 60:02d}:{value % 60:02d}"


# --------------------------------------------------------------- next change


def change_minutes(schedules: list[Schedule]) -> list[int]:
    """Every minute of the day at which the active sound could change.

    A schedule can only start or stop mattering on one of its own window
    edges, so those edges plus midnight are the complete set of moments worth
    testing -- scanning all 1440 minutes of 366 days would be pointless work.
    """
    minutes = {0}
    for s in schedules:
        if s.start_minute is not None:
            minutes.add(s.start_minute)
        if s.end_minute is not None:
            # The window is inclusive of its end minute, so the change lands
            # on the minute after it.
            minutes.add((s.end_minute + 1) % MINUTES_PER_DAY)
    return sorted(minutes)


def find_next_change(
    schedules: list[Schedule],
    default_mp3: Mp3File,
    now: datetime,
    *,
    horizon_days: int = 366,
) -> tuple[datetime, Resolution] | None:
    """When the resolved sound next changes, and what it changes to.

    Takes `now` rather than reading the clock, which is what makes it testable
    -- it lived in the dashboard route and called `now_local()` internally, so
    "what does this show on Christmas Eve" could only be asked by monkeypatching
    the clock.

    Stepping through candidate moments rather than whole days matters once
    time-of-day windows exist: a change is usually mid-afternoon, not midnight,
    and a date alone would be misleading.
    """
    current = resolve_active(schedules, default_mp3, now)
    current_mp3_id = current.mp3.id
    candidates = change_minutes(schedules)

    for offset in range(horizon_days):
        day = now.date() + timedelta(days=offset)
        midnight = datetime.combine(day, datetime.min.time())
        for minute in candidates:
            when = midnight + timedelta(minutes=minute)
            if when <= now:
                continue
            res = resolve_active(schedules, default_mp3, when)
            if res.mp3.id != current_mp3_id:
                return when, res
            current_mp3_id = res.mp3.id
    return None
