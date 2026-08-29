"""Local-time helpers.

Everything a human reads — audit rows, "last applied", log lines — is in the
configured local zone (`TZ`, default Europe/Brussels), not UTC. Timestamps
were previously written with `datetime.utcnow()`, which made the audit page
read an hour or two behind the wall clock and made correlating a chime with
an actual doorbell press needlessly annoying.

Stored values stay **naive**: SQLite has no timezone type, and the whole
application reads and writes in one zone. `now_local()` is therefore a naive
datetime that already reads as Brussels wall-clock time.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import DEFAULT_TIMEZONE, settings

log = logging.getLogger("doorbird.timezone")

_warned_about_zone = False


def local_zone() -> tzinfo:
    """The configured zone, or the default with a loud complaint.

    Silently substituting a zone would make every timestamp in the app wrong by
    a whole number of hours with nothing to explain it, so a bad `TZ` is logged
    once at ERROR rather than swallowed.
    """
    global _warned_about_zone
    try:
        return ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        if not _warned_about_zone:
            _warned_about_zone = True
            log.error(
                "TZ=%r is not a known timezone; falling back to %s. Every timestamp "
                "in the UI and the audit log will use that zone until TZ is fixed.",
                settings.timezone, DEFAULT_TIMEZONE,
            )
        return ZoneInfo(DEFAULT_TIMEZONE)


def now_local() -> datetime:
    """Wall-clock time in the configured zone, as a naive datetime."""
    return datetime.now(local_zone()).replace(tzinfo=None)


def utc_to_local(value: datetime) -> datetime:
    """Reinterpret a naive UTC timestamp as local wall-clock time."""

    aware = value.replace(tzinfo=UTC).astimezone(local_zone())
    return aware.replace(tzinfo=None)


def utc_offset_hours(at: datetime | None = None) -> float:
    """Current offset from UTC, used when migrating historic rows."""
    moment = at or datetime.now()
    offset = local_zone().utcoffset(moment) or timedelta(0)
    return offset.total_seconds() / 3600.0
