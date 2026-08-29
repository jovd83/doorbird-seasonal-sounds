from datetime import datetime, timedelta

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)

    mp3: Mapped["Mp3File"] = relationship()
    collection: Mapped["Mp3Collection | None"] = relationship(lazy="selectin")
    devices: Mapped[list["Device"]] = relationship(
        secondary=schedule_devices, lazy="selectin"
    )

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
