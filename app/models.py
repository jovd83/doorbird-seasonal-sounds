from datetime import date, datetime, timedelta

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app import holidays
from app.db import Base
from app.timezone import now_local

# Two kinds of sound, and two kinds of schedule that play them.
#
# A *chime* is what a visitor hears the moment they press the button. An
# *auto response* is a spoken message that follows it after a pause -- "you
# can leave the parcel on the porch". They are separated at the data level so
# a delivery message can never end up wired to the doorbell itself.
KIND_CHIME = "chime"
KIND_AUTO_RESPONSE = "auto_response"

KINDS: dict[str, str] = {
    KIND_CHIME: "Chime",
    KIND_AUTO_RESPONSE: "Auto response",
}

# Which door stations a schedule applies to. An empty set means "all of them",
# which keeps every schedule written before this table existed working.
schedule_devices = Table(
    "schedule_devices",
    Base.metadata,
    Column("schedule_id", ForeignKey("schedules.id", ondelete="CASCADE"), primary_key=True),
    Column("device_id", ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True),
)

# Which MP3s belong to a collection. A file may sit in several collections.
collection_mp3s = Table(
    "collection_mp3s",
    Base.metadata,
    Column("collection_id", ForeignKey("mp3_collections.id", ondelete="CASCADE"), primary_key=True),
    Column("mp3_id", ForeignKey("mp3_files.id", ondelete="CASCADE"), primary_key=True),
)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    host: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(120))
    password_enc: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    use_https: Mapped[bool] = mapped_column(Boolean, default=False)
    # Whether to check the TLS certificate when `use_https` is on. Off by
    # default because door stations ship self-signed certificates and are
    # addressed by IP, so verification cannot succeed on a stock device --
    # but it was previously not even expressible, and anyone who has put a
    # real certificate on theirs had no way to say so.
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    last_applied_mp3_id: Mapped[int | None] = mapped_column(ForeignKey("mp3_files.id"), nullable=True)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When this device last reported a ring, from whichever trigger source.
    # Stored rather than held on the watcher thread: the dashboard's headline
    # figure used to live in memory, so every restart reset it to "no rings
    # yet" while the audit log still listed the rings, and a ring arriving by
    # webhook never reached a watcher at all so it never showed up.
    last_ring_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)


class Mp3File(Base):
    __tablename__ = "mp3_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    sample_rate_hz: Mapped[int | None] = mapped_column(nullable=True)
    bitrate_kbps: Mapped[int | None] = mapped_column(nullable=True)
    # Which slot the file is for. Only chimes are eligible to be the default,
    # and a schedule may only reference a file of its own kind.
    kind: Mapped[str] = mapped_column(String(20), default=KIND_CHIME, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)

    @property
    def kind_label(self) -> str:
        return KINDS.get(self.kind, self.kind)


class Mp3Collection(Base):
    """A named bag of interchangeable sounds, e.g. three Christmas chimes.

    A schedule pointing at a collection plays a different member on each ring
    instead of the same file every time. Members are all of the collection's
    own kind, so a chime collection can never leak a spoken message into the
    doorbell.
    """

    __tablename__ = "mp3_collections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default=KIND_CHIME, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)

    mp3s: Mapped[list["Mp3File"]] = relationship(
        secondary=collection_mp3s, lazy="selectin"
    )

    @property
    def kind_label(self) -> str:
        return KINDS.get(self.kind, self.kind)


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (UniqueConstraint("name", name="uq_schedule_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    # Chime schedules decide what the doorbell sounds like; auto-response
    # schedules decide what is said afterwards. They resolve independently,
    # so one of each can be active at the same moment.
    kind: Mapped[str] = mapped_column(String(20), default=KIND_CHIME, index=True)
    # The sound to play. When `collection_id` is set the collection wins and a
    # member is drawn at each ring; `mp3_id` then holds one of its members, so
    # anything that just wants "a sound for this schedule" still has one.
    mp3_id: Mapped[int] = mapped_column(ForeignKey("mp3_files.id"))
    collection_id: Mapped[int | None] = mapped_column(
        ForeignKey("mp3_collections.id"), nullable=True)
    # Which of the three exclusive date rules applies: no restriction, the
    # month/day window below, or the ticked holidays. Everything written
    # before this column existed had a mandatory window, so `range` is the
    # default and the upgrade changes nothing about when a doorbell sounds.
    date_mode: Mapped[str] = mapped_column(
        String(20), default=holidays.DATE_RANGE,
        server_default=holidays.DATE_RANGE)
    # Only read in `range` mode. Kept NOT NULL and populated in every mode so
    # switching back and forth does not lose the dates someone typed.
    start_month: Mapped[int] = mapped_column(Integer)
    start_day: Mapped[int] = mapped_column(Integer)
    end_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Minutes since midnight. Both NULL means the schedule runs all day; a
    # start later than the end wraps past midnight (22:00 -> 02:00).
    start_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Auto responses only: how long to wait after the chime has finished
    # before the message is spoken. Ignored by chime schedules.
    delay_seconds: Mapped[int] = mapped_column(Integer, default=0)
    # Which weekdays the schedule may fire on, as a bitmask -- bit 0 is Monday.
    # 127 is every day, which is what every schedule written before this column
    # existed is migrated to, so the upgrade changes nothing about when a
    # doorbell sounds.
    weekday_mask: Mapped[int] = mapped_column(
        Integer, default=holidays.ALL_DAYS, server_default=str(holidays.ALL_DAYS))
    # Drops a day that matched only by its weekday when it is one of the ten
    # federal public holidays. Never touches a holiday ticked explicitly.
    skip_public_holidays: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)

    mp3: Mapped["Mp3File"] = relationship()
    collection: Mapped["Mp3Collection | None"] = relationship(lazy="selectin")
    devices: Mapped[list["Device"]] = relationship(
        secondary=schedule_devices, lazy="selectin"
    )
    holiday_rows: Mapped[list["ScheduleHoliday"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def holiday_keys(self) -> frozenset[str]:
        """The holidays this schedule fires on regardless of weekday."""
        return frozenset(r.holiday_key for r in self.holiday_rows)

    def set_holiday_keys(self, keys) -> None:
        """Replace the selection, dropping anything not in the catalogue.

        Unknown keys are discarded rather than raising: the catalogue is code,
        so a key can only go missing when a holiday is removed from it in an
        upgrade, and a schedule that outlives one entry should keep working
        with the rest of its selection intact.

        The collection is edited in place rather than reassigned. Handing the
        relationship a fresh list of equivalent rows makes SQLAlchemy delete
        and re-insert every key on every save, and within a single flush the
        INSERT can be ordered ahead of the DELETE — which the composite primary
        key then rejects. Rows that are staying simply stay.
        """
        wanted = {k for k in keys if k in holidays.VALID_KEYS}
        current = {row.holiday_key: row for row in self.holiday_rows}

        for key, row in current.items():
            if key not in wanted:
                self.holiday_rows.remove(row)
        for key in sorted(wanted - set(current)):
            self.holiday_rows.append(ScheduleHoliday(holiday_key=key))

    @property
    def holiday_names(self) -> list[str]:
        return holidays.names(self.holiday_keys)

    @property
    def days_label(self) -> str:
        return holidays.describe_days(self.weekday_mask)

    @property
    def date_mode_key(self) -> str:
        """The stored mode, with anything unrecognised reading as a range."""
        return holidays.effective_date_mode(self.date_mode)

    @property
    def is_holidays_mode(self) -> bool:
        """True when the ticked holidays are the whole date rule.

        The Days control is inert in this state -- `matches_day` returns True
        without consulting the mask -- so the row greys it out rather than
        showing a rule nothing honours.
        """
        return self.date_mode_key == holidays.DATE_HOLIDAYS

    @property
    def is_every_weekday(self) -> bool:
        """True when the *weekday* rule is the default one.

        Drives which way the Days column's All/Custom radio sits, so it asks
        only about what that column owns: the seven weekdays and the skip.
        Holidays are deliberately not consulted -- they are edited in the
        Dates column, and a schedule that fires every weekday plus Christmas
        still has a default weekday rule.

        Not a stored column: 'every weekday, nothing skipped' is a state the
        other two fields already describe, and a third field saying the same
        thing is a third field that can disagree with them.
        """
        return (
            holidays.effective_mask(self.weekday_mask) == holidays.ALL_DAYS
            and not self.skip_public_holidays
        )

    @property
    def day_summary(self) -> str:
        """'Mo–Fr · skipping' — what the Days cell says.

        Weekdays only. The holidays moved to the Dates column and are
        summarised by `holiday_summary`, so this string and that one describe
        two separate controls and must not both claim the same fact.

        The editor's script rebuilds this same string as you edit, so the two
        have to agree; keeping the server's version here rather than in the
        template is what makes that pair checkable.
        """
        if self.is_every_weekday:
            return "Every day"
        parts = [holidays.describe_days(self.weekday_mask)]
        if self.skip_public_holidays and holidays.effective_mask(self.weekday_mask):
            parts.append("skipping")
        return " · ".join(parts)

    def day_on(self, weekday: int) -> bool:
        """Template helper: is this weekday ticked? Monday is 0."""
        return holidays.day_selected(self.weekday_mask, weekday)

    @property
    def holiday_summary(self) -> str:
        """What the Dates cell says when the mode is Holidays.

        Names the holiday outright when there is only one -- 'Christmas Day'
        reads, '1 holiday' does not. Says nothing about the skip: that lives
        in the Days column now, and is reported by `day_summary`.
        """
        chosen = self.holiday_names
        if not chosen:
            return "No holidays"
        return holidays.summarise_names(chosen)

    @property
    def date_summary(self) -> str:
        """One line for the Dates cell, whichever of the three modes is on."""
        mode = self.date_mode_key
        if mode == holidays.DATE_ALWAYS:
            return "Always"
        if mode == holidays.DATE_HOLIDAYS:
            return self.holiday_summary
        end_m = self.end_month or self.start_month
        end_d = self.end_day or self.start_day
        start = holidays.short_date(self.start_month, self.start_day)
        end = holidays.short_date(end_m, end_d)
        return start if start == end else f"{start} – {end}"

    @property
    def all_day(self) -> bool:
        return self.start_minute is None and self.end_minute is None

    @property
    def kind_label(self) -> str:
        return KINDS.get(self.kind, self.kind)

    @property
    def sound_label(self) -> str:
        """What the UI shows in a 'sound' column, collection or single file."""
        if self.collection is not None:
            return f"{self.collection.name} ({len(self.collection.mp3s)} random)"
        return self.mp3.label

    def applies_to(self, device_id: int | None) -> bool:
        """An empty device list means the schedule covers every door station."""
        if not self.devices or device_id is None:
            return True
        return any(d.id == device_id for d in self.devices)


class ScheduleHoliday(Base):
    """One holiday a schedule fires on, whatever weekday it lands on.

    The key is a catalogue constant from `app.holidays`, not a foreign key:
    the twenty entries are code, not data, so there is no table for them to
    point at and nothing for a user to add or rename.
    """

    __tablename__ = "schedule_holidays"

    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("schedules.id", ondelete="CASCADE"), primary_key=True)
    holiday_key: Mapped[str] = mapped_column(String(40), primary_key=True)


class HolidayDate(Base):
    """A materialised date for a holiday that moves with Easter.

    Fixed holidays are absent by design -- 25 December needs no row. The five
    that move are written out a century ahead by `app.holiday_store` so the
    reference page can show real dates and nothing has to run a date algorithm
    to answer a question about next year.
    """

    __tablename__ = "holiday_dates"

    holiday_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    on_date: Mapped[date] = mapped_column(Date, primary_key=True, index=True)


class AppSetting(Base):
    """Key/value store for settings the user changes from the web UI.

    Kept separate from `.env`: environment variables are the deployment's
    defaults, but the ring trigger has to be switchable at runtime without a
    container restart, and the webhook token has to be generated and persisted
    on first boot.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id"), nullable=True)
    mp3_id: Mapped[int | None] = mapped_column(ForeignKey("mp3_files.id"), nullable=True)
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("schedules.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(40))
    success: Mapped[bool] = mapped_column(Boolean)
    message: Mapped[str] = mapped_column(Text, default="")


def prune_audit_log(db, *, older_than_days: int, now: datetime | None = None) -> int:
    """Delete audit rows past the retention window. Returns how many went.

    Every ring writes one or two rows and nothing ever removed them: the only
    pruning was a manual "Clear log" button, so the table grew without bound
    on a busy front door and the audit page's COUNT(*) scanned all of it.

    `older_than_days <= 0` disables pruning entirely, which is the escape hatch
    for anyone who wants to keep the lot.
    """
    if older_than_days <= 0:
        return 0
    cutoff = (now or now_local()) - timedelta(days=older_than_days)
    return (
        db.query(AuditLog)
        .filter(AuditLog.ts < cutoff)
        .delete(synchronize_session=False)
    )
