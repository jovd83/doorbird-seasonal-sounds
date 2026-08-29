import logging
import random
import threading
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.crypto import decrypt
from app.date_logic import Resolution, pick_schedule, resolve_active
from app.doorbird import DeviceCreds, DoorBirdClient, DoorBirdError
from app.models import KIND_AUTO_RESPONSE, KIND_CHIME, AuditLog, Device, Mp3File, Schedule
from app.timezone import now_local

log = logging.getLogger("doorbird.engine")


def current_default_mp3(db: Session) -> Mp3File | None:
    return db.query(Mp3File).filter(Mp3File.is_default.is_(True)).first()


def chime_schedules(db: Session) -> list[Schedule]:
    return db.query(Schedule).filter(Schedule.kind == KIND_CHIME).all()


def auto_response_schedules(db: Session) -> list[Schedule]:
    return db.query(Schedule).filter(Schedule.kind == KIND_AUTO_RESPONSE).all()


def today_resolution(
    db: Session,
    today: date | datetime | None = None,
    device_id: int | None = None,
) -> Resolution | None:
    """What should play right now, optionally narrowed to one device.

    Defaults to *now* rather than midnight so time-of-day windows resolve
    correctly; pass an explicit date to ask "what would play that day".
    """
    when = today or now_local()
    default = current_default_mp3(db)
    if default is None:
        return None
    return resolve_active(chime_schedules(db), default, when, device_id)


def active_auto_response(
    db: Session,
    when: date | datetime | None = None,
    device_id: int | None = None,
) -> Schedule | None:
    """The auto-response schedule due right now, if any.

    Unlike the chime there is no default to fall back on: a spoken message is
    only ever played when a schedule explicitly asks for it.
    """
    return pick_schedule(auto_response_schedules(db), when or now_local(), device_id)


def mp3_path(mp3: Mp3File) -> Path:
    return settings.mp3_dir / mp3.filename


# Last file drawn per schedule, so a collection does not play the same sound
# twice running. In memory only: after a restart the first draw is free, which
# is the right trade for not writing to the database on every ring.
_last_pick: dict[int, int] = {}
_pick_lock = threading.Lock()

# Held as an instance rather than reaching for the `random` module directly, so
# a test can substitute a seeded one and get a deterministic sequence. Sharing
# the global module state made the collection tests genuinely flaky: with three
# members and six draws, "every member was used" fails about 6% of the time by
# chance alone.
# Not a security decision -- this picks which jingle plays.
_rng = random.Random()  # noqa: S311


def pick_sound(
    schedule: Schedule | None,
    fallback: Mp3File,
    *,
    rng: random.Random | None = None,
) -> Mp3File:
    """The actual file to play now — a random collection member, or the one file.

    Deliberately called at play time rather than during resolution: the
    dashboard must show a stable answer, while two rings a minute apart should
    genuinely differ. With three Christmas chimes, pure chance would repeat
    one about a third of the time and read as broken, so the previous draw is
    excluded whenever there is anything else to choose from.
    """
    if schedule is None or schedule.collection is None:
        return fallback
    members = list(schedule.collection.mp3s)
    if not members:
        return fallback
    if len(members) == 1:
        return members[0]

    chooser = rng or _rng
    with _pick_lock:
        previous = _last_pick.get(schedule.id)
        choices = [m for m in members if m.id != previous] or members
        chosen = chooser.choice(choices)
        _last_pick[schedule.id] = chosen.id
    return chosen


def _audit(
    db: Session,
    *,
    action: str,
    success: bool,
    message: str = "",
    device_id: int | None = None,
    mp3_id: int | None = None,
    schedule_id: int | None = None,
) -> None:
    db.add(AuditLog(
        action=action,
        success=success,
        message=message,
        device_id=device_id,
        mp3_id=mp3_id,
        schedule_id=schedule_id,
    ))


def apply_to_device(db: Session, device: Device, mp3: Mp3File, *, schedule: Schedule | None = None,
                    force: bool = False) -> tuple[bool, str]:
    if not device.enabled:
        return False, "device disabled"
    if device.last_applied_mp3_id == mp3.id and not force:
        return True, "already current (skipped)"

    creds = DeviceCreds(
        host=device.host,
        username=device.username,
        password=decrypt(device.password_enc),
        use_https=device.use_https,
        verify_tls=device.verify_tls,
    )
    path = mp3_path(mp3)
    if not path.exists():
        msg = f"MP3 file missing on disk: {path}"
        _audit(db, action="apply", success=False, message=msg,
               device_id=device.id, mp3_id=mp3.id, schedule_id=schedule.id if schedule else None)
        device.last_error = msg
        return False, msg

    try:
        with DoorBirdClient(creds) as client:
            result = client.set_button_sound(path)
    except DoorBirdError as exc:
        msg = f"upload failed: {exc}"
        log.error("apply_to_device failed (%s): %s", device.name, msg)
        _audit(db, action="apply", success=False, message=msg,
               device_id=device.id, mp3_id=mp3.id, schedule_id=schedule.id if schedule else None)
        device.last_error = msg
        return False, msg
    except Exception as exc:
        msg = f"unexpected error: {exc!r}"
        log.exception("apply_to_device crash (%s)", device.name)
        _audit(db, action="apply", success=False, message=msg,
               device_id=device.id, mp3_id=mp3.id, schedule_id=schedule.id if schedule else None)
        device.last_error = msg
        return False, msg

    device.last_applied_mp3_id = mp3.id
    device.last_applied_at = now_local()
    device.last_error = None
    _audit(db, action="apply", success=True, message=result,
           device_id=device.id, mp3_id=mp3.id, schedule_id=schedule.id if schedule else None)
    return True, result


def reconcile(db: Session, *, force: bool = False) -> dict:
    overall = today_resolution(db)
    if overall is None:
        log.warning("reconcile: no default mp3 set; nothing to do")
        return {"status": "no-default", "applied": 0, "failed": 0, "details": []}

    devices = db.query(Device).filter(Device.enabled.is_(True)).all()
    applied = 0
    failed = 0
    details = []
    for d in devices:
        # Each device resolves separately: a schedule may target only some.
        res = today_resolution(db, device_id=d.id) or overall
        sound = pick_sound(res.schedule, res.mp3)
        ok, msg = apply_to_device(db, d, sound, schedule=res.schedule, force=force)
        details.append({"device": d.name, "ok": ok, "message": msg})
        if ok:
            applied += 1
        else:
            failed += 1
    db.flush()
    res = overall
    return {
        "status": "ok",
        "applied": applied,
        "failed": failed,
        "active_mp3": res.schedule.sound_label if res.schedule else res.mp3.label,
        "reason": res.reason,
        "schedule": res.schedule.name if res.schedule else None,
        "details": details,
    }
