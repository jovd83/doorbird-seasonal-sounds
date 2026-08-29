"""Playing a chime, and the spoken message that may follow it.

Separated from the watcher threads because these are what every trigger source
ends up calling -- the monitor stream, the ring webhook, and the "test chime"
button in the UI all arrive here.
"""
from __future__ import annotations

import logging
import threading
import time

from app import settings_store
from app.audio import ensure_ulaw
from app.crypto import decrypt
from app.db import session_scope
from app.doorbird import DeviceCreds, DoorBirdClient
from app.engine import active_auto_response, mp3_path, pick_sound, today_resolution
from app.models import AuditLog, Device

log = logging.getLogger("doorbird.ring")

# One pending auto response per device, each holding a cancel flag. Without
# this every ring parked a thread on `time.sleep(delay)` -- up to an hour at
# MAX_DELAY_SECONDS -- with no way to cancel it or even see it.
_pending_auto: dict[int, threading.Event] = {}
_auto_lock = threading.Lock()


def play_active_chime(device_id: int) -> tuple[bool, str]:
    """Resolve today's MP3 and play it on one device. Audited either way."""
    with session_scope() as db:
        device = db.get(Device, device_id)
        if device is None:
            return False, "device no longer exists"
        if not device.enabled:
            return False, "device disabled"

        res = today_resolution(db, device_id=device_id)
        if res is None:
            msg = "no default MP3 set - upload one and mark it default"
            db.add(AuditLog(action="chime", success=False, message=msg, device_id=device_id))
            return False, msg

        # `schedule is None` means nothing matched and we fell back to the
        # default. Some installs would rather hear only the door station's own
        # chime off-season, so that fallback is switchable.
        if res.schedule is None and not settings_store.play_default_when_idle():
            msg = "no schedule active - staying silent (default playback is off)"
            db.add(AuditLog(action="chime", success=True, message=msg,
                            device_id=device_id, mp3_id=None))
            silent = True
        else:
            silent = False

        creds = DeviceCreds(
            host=device.host,
            username=device.username,
            password=decrypt(device.password_enc),
            use_https=device.use_https,
            verify_tls=device.verify_tls,
        )
        # A schedule backed by a collection draws a fresh member per ring.
        sound = pick_sound(res.schedule, res.mp3)
        source = mp3_path(sound)
        mp3_id = sound.id
        schedule_id = res.schedule.id if res.schedule else None
        label, reason = sound.label, res.reason
        name = device.name

    # Staying silent still leaves the auto response due: the two are
    # independent, and a delivery message is useful even with no chime.
    if silent:
        schedule_auto_response(device_id)
        return True, msg

    # Transcode + play outside the DB session: both can take seconds.
    try:
        ulaw = ensure_ulaw(source)
        with DoorBirdClient(creds) as client:
            result = client.play_ulaw(ulaw)
        ok, msg = True, f"{result} [{label} - {reason}]"
    except Exception as exc:
        ok, msg = False, f"chime failed on {name}: {exc}"

    with session_scope() as db:
        db.add(AuditLog(action="chime", success=ok, message=msg,
                        device_id=device_id, mp3_id=mp3_id, schedule_id=schedule_id))
        device = db.get(Device, device_id)
        if device is not None:
            device.last_error = None if ok else msg

    schedule_auto_response(device_id)
    return ok, msg


# ------------------------------------------------------------ auto response


def schedule_auto_response(device_id: int) -> bool:
    """Queue the spoken follow-up, if one is due. Returns True if queued.

    Always off the calling thread: the wait interval is measured from the
    moment the chime finished, and whoever triggered the ring — the webhook,
    the "test chime" button — must not be held open for it.

    One pending response per device. A second ring while one is still waiting
    replaces it rather than stacking: the message is about the visitor at the
    door, and saying it twice over is worse than saying it once. This also caps
    the resource cost, which used to be one parked OS thread per ring for up to
    an hour each.
    """
    with session_scope() as db:
        due = active_auto_response(db, device_id=device_id) is not None
    if not due:
        return False

    with _auto_lock:
        previous = _pending_auto.get(device_id)
        if previous is not None:
            # Tell the earlier one to abandon its wait.
            previous.set()
        cancel = threading.Event()
        _pending_auto[device_id] = cancel

    threading.Thread(
        target=_play_auto_response_safely, args=(device_id, cancel),
        name=f"auto-response-{device_id}", daemon=True,
    ).start()
    return True


def cancel_pending_auto_responses() -> None:
    """Abandon every waiting auto response. Used on shutdown."""
    with _auto_lock:
        pending = list(_pending_auto.values())
        _pending_auto.clear()
    for cancel in pending:
        cancel.set()


def _wait_out_delay(delay: float, cancel: threading.Event | None) -> bool:
    """Hold for `delay` seconds. True if the wait was cut short.

    An interruptible wait rather than `time.sleep`: a thread parked for up to
    an hour with no way to be told to stop outlives whatever asked for it, and
    cannot be seen from the outside. Also the seam the tests reach for, so they
    do not have to sit through a real interval.
    """
    if delay <= 0:
        return False
    if cancel is not None:
        return cancel.wait(delay)
    time.sleep(delay)
    return False


def _play_auto_response_safely(device_id: int, cancel: threading.Event | None = None) -> None:
    try:
        ok, msg = play_auto_response(device_id, cancel=cancel)
        log.log(logging.INFO if ok else logging.WARNING,
                "[device %s] auto response: %s", device_id, msg)
    except Exception:
        log.exception("[device %s] auto response crashed", device_id)
    finally:
        with _auto_lock:
            if cancel is not None and _pending_auto.get(device_id) is cancel:
                _pending_auto.pop(device_id, None)


def play_auto_response(
    device_id: int, cancel: threading.Event | None = None
) -> tuple[bool, str]:
    """Wait out the schedule's interval, then speak its message. Audited.

    `cancel` cuts the wait short -- set when a newer ring supersedes this one,
    or when the app is shutting down.
    """
    with session_scope() as db:
        device = db.get(Device, device_id)
        if device is None:
            return False, "device no longer exists"
        if not device.enabled:
            return False, "device disabled"

        schedule = active_auto_response(db, device_id=device_id)
        if schedule is None:
            return False, "no auto response active"

        creds = DeviceCreds(
            host=device.host,
            username=device.username,
            password=decrypt(device.password_enc),
            use_https=device.use_https,
            verify_tls=device.verify_tls,
        )
        sound = pick_sound(schedule, schedule.mp3)
        source = mp3_path(sound)
        mp3_id = sound.id
        schedule_id = schedule.id
        label = sound.label
        delay = max(0, schedule.delay_seconds or 0)
        sched_name = schedule.name
        name = device.name

    if delay:
        log.info("[%s] auto response %r in %ss", name, sched_name, delay)
        if _wait_out_delay(delay, cancel):
            return False, "superseded by a newer ring"

    try:
        ulaw = ensure_ulaw(source)
        with DoorBirdClient(creds) as client:
            result = client.play_ulaw(ulaw)
        ok, msg = True, f"{result} [{label} - auto response '{sched_name}' after {delay}s]"
    except Exception as exc:
        ok, msg = False, f"auto response failed on {name}: {exc}"

    with session_scope() as db:
        db.add(AuditLog(action="auto-response", success=ok, message=msg,
                        device_id=device_id, mp3_id=mp3_id, schedule_id=schedule_id))
    return ok, msg


