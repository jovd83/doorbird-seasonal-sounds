"""Belgian holidays, and the weekday bitmask schedules match against.

Two kinds of entry live in one catalogue. Most are a fixed month and day and
need nothing stored — 25 December is 25 December. Five of them move with
Easter, and those are the reason this module exists: their dates are computed
here once and materialised into `holiday_dates` for a century ahead (see
`app.holiday_store`), so answering "is today a holiday?" on a ring is a lookup
rather than a calculation on a thread that should be pushing audio.

The rules themselves stay pure and dependency-free on purpose. They are
vendored into the Home Assistant component alongside `date_logic`, which cannot
import from the app, and `tests/test_ha_agreement.py` fails if the two copies
ever disagree.

Groups matter to more than presentation. Only `GROUP_PUBLIC` counts for a
schedule's "skip public holidays" toggle; community days and observances never
subtract a day, they only ever add one when explicitly ticked.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

# --------------------------------------------------------------- weekdays

# Bit i of a schedule's mask is `date.weekday() == i`, so Monday is bit 0 and
# Sunday is bit 6. Stored as a single integer because a schedule matching
# "Mon, Wed, Fri" is one column and one AND, not a seven-row join.
MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)

WEEKDAY_LABELS: tuple[str, ...] = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
WEEKDAY_NAMES: tuple[str, ...] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# --------------------------------------------------------------- date mode

# How a schedule decides *which dates* it covers. The three are mutually
# exclusive: a schedule is either unrestricted, or bounded by a calendar
# window, or pinned to a set of named holidays. They used to be entangled --
# the window was mandatory and holidays merely added days inside it -- which
# made "only on Christmas" impossible to say without also inventing a range.
DATE_ALWAYS = "always"
DATE_RANGE = "range"
DATE_HOLIDAYS = "holidays"
DATE_MODES: tuple[str, ...] = (DATE_ALWAYS, DATE_RANGE, DATE_HOLIDAYS)

DATE_MODE_LABELS: dict[str, str] = {
    DATE_ALWAYS: "Always",
    DATE_RANGE: "Date interval",
    DATE_HOLIDAYS: "Holidays",
}


def effective_date_mode(mode: str | None) -> str:
    """The mode to actually match on, with anything unknown reading as a range.

    Every schedule written before the column existed has a stored window and
    no mode, and a window is exactly what `range` means, so that is the safe
    reading -- and it keeps the matcher total rather than raising on a row
    that is perfectly valid.
    """
    return mode if mode in DATE_MODES else DATE_RANGE


ALL_DAYS = 0b1111111        # 127 — every schedule written before this existed
WEEKDAYS = 0b0011111        # 31  — Mon–Fri
WEEKEND = 0b1100000         # 96  — Sat–Sun
NO_DAYS = 0

# The presets the form offers, in the order they are shown.
DAY_PRESETS: tuple[tuple[str, int], ...] = (
    ("Every day", ALL_DAYS),
    ("Weekdays · Mo–Fr", WEEKDAYS),
    ("Weekend · Sa–Su", WEEKEND),
    ("None", NO_DAYS),
)


def effective_mask(mask: int | None) -> int:
    """The mask to actually match against, with `None` reading as every day.

    A `Schedule` that has not been flushed yet carries `None` here -- column
    defaults are applied by the INSERT, not by the constructor -- and so does
    any row that predates the column. Both mean "no day rule", and no day rule
    means every day, so the matcher stays total rather than raising on an
    object that is perfectly valid.
    """
    return ALL_DAYS if mask is None else mask & ALL_DAYS


def day_selected(mask: int | None, weekday: int) -> bool:
    return bool(effective_mask(mask) >> weekday & 1)


def describe_days(mask: int | None) -> str:
    """'Mo–Fr', 'Every day', 'Mo, We, Fr' — what the row and summary show."""
    mask = effective_mask(mask)
    if mask == ALL_DAYS:
        return "Every day"
    if mask == WEEKDAYS:
        return "Mo–Fr"
    if mask == WEEKEND:
        return "Sa–Su"
    if mask == NO_DAYS:
        return "No weekday"
    return ", ".join(WEEKDAY_LABELS[i] for i in range(7) if day_selected(mask, i))


def day_count(mask: int | None) -> int:
    """How many weekdays the mask covers; fewer is more specific."""
    return bin(effective_mask(mask)).count("1")


# --------------------------------------------------------------- catalogue

GROUP_PUBLIC = "public"
GROUP_COMMUNITY = "community"
GROUP_OBSERVANCE = "observance"

GROUP_LABELS: dict[str, str] = {
    GROUP_PUBLIC: "Public holidays",
    GROUP_COMMUNITY: "Community days",
    GROUP_OBSERVANCE: "Observances",
}
GROUP_ORDER: tuple[str, ...] = (GROUP_PUBLIC, GROUP_COMMUNITY, GROUP_OBSERVANCE)


@dataclass(frozen=True)
class Holiday:
    """One entry in the catalogue.

    Exactly one of `month`/`day` or `easter_offset` is set. `easter_offset` is
    in days from Easter Sunday, so 0 is Easter itself and 1 is Easter Monday.
    """

    key: str
    name: str
    group: str
    month: int | None = None
    day: int | None = None
    easter_offset: int | None = None

    @property
    def moves(self) -> bool:
        return self.easter_offset is not None

    @property
    def rule(self) -> str:
        """How the date is arrived at, for the reference page."""
        if self.easter_offset is None:
            return f"{self.day} {_MONTH_NAMES[self.month - 1]}"
        if self.easter_offset == 0:
            return "Easter"
        unit = "day" if self.easter_offset == 1 else "days"
        return f"Easter + {self.easter_offset} {unit}"

    @property
    def short_rule(self) -> str:
        """'25 Dec' for a fixed date; moveable ones have no short form."""
        if self.easter_offset is not None:
            return ""
        return f"{self.day} {_MONTH_NAMES[self.month - 1][:3]}"


# How many characters of holiday names the Dates column shows before the list
# has to be trimmed. The 11rem column fits roughly 29 characters a line and
# the summary is allowed to wrap once. The row's script is handed this same
# number, so the two never disagree about where to cut.
NAME_LIST_BUDGET = 44


def _and_others(count: int) -> str:
    return f" and {count} other" + ("s" if count != 1 else "")


def summarise_names(names, budget: int = NAME_LIST_BUDGET) -> str:
    """'Christmas Day, Sinterklaas' while it fits, else '... and 3 others'.

    Naming the days beats counting them -- '2 holidays' tells you nothing you
    could act on -- so the whole list is shown whenever it fits the column,
    and only the overflow is folded away. At least one name is always kept,
    however long it is, because a bare "and 4 others" names nothing at all.
    """
    names = list(names)
    if not names:
        return ""
    joined = ", ".join(names)
    if len(joined) <= budget:
        return joined

    shown: list[str] = []
    for name in names:
        remaining = len(names) - len(shown) - 1
        candidate = ", ".join([*shown, name])
        if remaining:
            candidate += _and_others(remaining)
        if shown and len(candidate) > budget:
            break
        shown.append(name)

    left = len(names) - len(shown)
    return ", ".join(shown) + (_and_others(left) if left else "")


def short_date(month: int, day: int) -> str:
    """'25 Dec' -- the compact form the schedule rows use for a date."""
    return f"{day} {_MONTH_NAMES[month - 1][:3]}"


_MONTH_NAMES = ("January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December")


# The ten federal public holidays, the three community days, and the six
# observances a doorbell has any reason to care about. Order within a group is
# calendar order for the fixed ones, with the moveable ones sitting where they
# usually fall.
HOLIDAYS: tuple[Holiday, ...] = (
    # --- the ten federal public holidays -------------------------------
    Holiday("new_year", "New Year's Day", GROUP_PUBLIC, month=1, day=1),
    Holiday("easter_monday", "Easter Monday", GROUP_PUBLIC, easter_offset=1),
    Holiday("labour_day", "Labour Day", GROUP_PUBLIC, month=5, day=1),
    Holiday("ascension", "Ascension Day", GROUP_PUBLIC, easter_offset=39),
    Holiday("whit_monday", "Whit Monday", GROUP_PUBLIC, easter_offset=50),
    Holiday("national_day", "National Day", GROUP_PUBLIC, month=7, day=21),
    Holiday("assumption", "Assumption", GROUP_PUBLIC, month=8, day=15),
    Holiday("all_saints", "All Saints' Day", GROUP_PUBLIC, month=11, day=1),
    Holiday("armistice", "Armistice Day", GROUP_PUBLIC, month=11, day=11),
    Holiday("christmas", "Christmas Day", GROUP_PUBLIC, month=12, day=25),
    # --- community days -------------------------------------------------
    Holiday("flemish_community", "Flemish Community", GROUP_COMMUNITY, month=7, day=11),
    Holiday("french_community", "French Community", GROUP_COMMUNITY, month=9, day=27),
    Holiday("german_community", "German-speaking Community", GROUP_COMMUNITY, month=11, day=15),
    # --- observances ----------------------------------------------------
    Holiday("valentines", "Valentine's Day", GROUP_OBSERVANCE, month=2, day=14),
    Holiday("easter_sunday", "Easter Sunday", GROUP_OBSERVANCE, easter_offset=0),
    Holiday("whit_sunday", "Whit Sunday", GROUP_OBSERVANCE, easter_offset=49),
    Holiday("halloween", "Halloween", GROUP_OBSERVANCE, month=10, day=31),
    Holiday("sinterklaas", "Sinterklaas", GROUP_OBSERVANCE, month=12, day=6),
    # Tweede Kerstdag. An observance rather than a public holiday: 26 December
    # is a federal holiday in the Netherlands and Germany but not in Belgium,
    # and GROUP_PUBLIC is what "skip public holidays" subtracts, so filing it
    # there would quietly start dropping Boxing Day from Mo-Fr schedules.
    Holiday("second_christmas", "2nd Christmas", GROUP_OBSERVANCE, month=12, day=26),
    Holiday("new_years_eve", "New Year's Eve", GROUP_OBSERVANCE, month=12, day=31),
)

BY_KEY: dict[str, Holiday] = {h.key: h for h in HOLIDAYS}
VALID_KEYS: frozenset[str] = frozenset(BY_KEY)
PUBLIC_KEYS: frozenset[str] = frozenset(h.key for h in HOLIDAYS if h.group == GROUP_PUBLIC)
MOVING: tuple[Holiday, ...] = tuple(h for h in HOLIDAYS if h.moves)
MOVING_KEYS: frozenset[str] = frozenset(h.key for h in MOVING)


def grouped() -> list[tuple[str, str, list[Holiday]]]:
    """(key, label, members) per group, in display order."""
    return [
        (g, GROUP_LABELS[g], [h for h in HOLIDAYS if h.group == g])
        for g in GROUP_ORDER
    ]


def names(keys) -> list[str]:
    """Catalogue order, not selection order, so two schedules read alike."""
    chosen = set(keys)
    return [h.name for h in HOLIDAYS if h.key in chosen]


# ------------------------------------------------------------------ dates

# How far ahead the stored table is kept. A hundred years is what was asked
# for, and it is only 500 rows -- five moveable holidays times a century.
HORIZON_YEARS = 100


def easter(year: int) -> date:
    """Easter Sunday in the Gregorian calendar (Meeus/Jones/Butcher).

    Integer arithmetic only, no table, valid for every year the `date` type
    accepts. Written out rather than pulled from a dependency because this file
    is vendored into a Home Assistant component that installs nothing.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=256)
def occurrences(year: int) -> dict[str, date]:
    """Every holiday in the catalogue, dated for one year.

    Cached per year: a resolver walking 366 days to find the next change would
    otherwise recompute Easter for the same year hundreds of times.
    """
    sunday = easter(year)
    dated: dict[str, date] = {}
    for h in HOLIDAYS:
        if h.easter_offset is None:
            dated[h.key] = date(year, h.month, h.day)
        else:
            dated[h.key] = sunday + timedelta(days=h.easter_offset)
    return dated


@lru_cache(maxsize=4096)
def keys_on(day: date) -> frozenset[str]:
    """Which holidays fall on this date. Empty on an ordinary day."""
    return frozenset(k for k, d in occurrences(day.year).items() if d == day)


def is_public_holiday(day: date) -> bool:
    """True on one of the ten federal days — what the skip toggle acts on."""
    return bool(keys_on(day) & PUBLIC_KEYS)


def next_occurrence(key: str, on_or_after: date) -> date:
    """The next time this holiday comes round, counting today as next."""
    this_year = occurrences(on_or_after.year)[key]
    if this_year >= on_or_after:
        return this_year
    return occurrences(on_or_after.year + 1)[key]


def moving_dates(first_year: int, years: int = HORIZON_YEARS) -> list[tuple[str, date]]:
    """(key, date) for every moveable holiday across a span of years.

    This is exactly what `holiday_dates` holds; the store writes what this
    returns, and a test asserts the table and this function never drift apart.
    """
    rows: list[tuple[str, date]] = []
    for year in range(first_year, first_year + years):
        dated = occurrences(year)
        rows.extend((h.key, dated[h.key]) for h in MOVING)
    return rows
