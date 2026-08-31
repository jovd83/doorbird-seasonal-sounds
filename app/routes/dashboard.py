"""The dashboard: what is playing now, and what changes next.

The "next change" scan lives in `app.date_logic` with the rest of the pure
schedule logic. It used to sit here and read the clock itself, which made it
untestable for any moment other than the present.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import ring, settings_store
from app.config import settings
from app.date_logic import find_next_change
from app.db import get_db, session_scope
from app.engine import (
    active_auto_response,
    auto_response_schedules,
    chime_schedules,
    current_default_mp3,
    reconcile,
    today_resolution,
)
from app.models import Device, Mp3File, Schedule
from app.security import require_auth, require_csrf
from app.shell import resolve_shell
from app.templating import templates
from app.timezone import now_local

router = APIRouter(
    dependencies=[Depends(require_auth), Depends(require_csrf), Depends(resolve_shell)],
)


@router.get("/dashboard")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    # Resolved once and passed down, so every panel on the page describes the
    # same moment even if the render straddles a minute boundary.
    now = now_local()
    devices = db.query(Device).order_by(Device.name).all()
    chimes = sorted(chime_schedules(db), key=lambda s: (-s.priority, s.name))
    auto_responses = sorted(auto_response_schedules(db), key=lambda s: (-s.priority, s.name))
    default = current_default_mp3(db)
    resolution = today_resolution(db, now)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "devices": devices,
            "schedules": chimes,
            "auto_responses": auto_responses,
            "active_auto_response": active_auto_response(db, now),
            "default_mp3": default,
            "resolution": resolution,
            "today": now.date(),
            "now": now,
            "next_change": _next_change(chimes, default, now),
            "ring_chime_enabled": settings.ring_chime_enabled,
            "silent_today": (resolution is not None
                             and resolution.schedule is None
                             and not settings_store.play_default_when_idle()),
            "ring_status": ring.status(),
            # Read off the devices rather than the watchers: a ring arriving
            # by webhook never touches a watcher, and in external-trigger mode
            # there are none running at all.
            "last_ring_device": _last_ring_device(devices),
            # Keyed by device id so the listener rows can show a ring time
            # without `status()` having to open a session of its own.
            "last_rings": {d.id: d.last_ring_at for d in devices},
        },
    )


def _last_ring_device(devices: list[Device]) -> Device | None:
    """Whichever device rang most recently, or None if none ever has."""
    rung = [d for d in devices if d.last_ring_at is not None]
    return max(rung, key=lambda d: d.last_ring_at) if rung else None


def _next_change(
    schedules: list[Schedule], default: Mp3File | None, now: datetime
) -> tuple[datetime, str] | None:
    """The next change, phrased for the page."""
    if default is None:
        return None
    found = find_next_change(schedules, default, now)
    if found is None:
        return None

    when, res = found
    if res.schedule is None:
        label, sound = "back to default", res.mp3.label
    else:
        label = f"schedule '{res.schedule.name}' starts"
        # A collection-backed schedule has no single next sound, so name the
        # collection rather than whichever member happens to be on the row.
        sound = res.schedule.sound_label
    return when, f"{label} → {sound}"


@router.post("/apply-now")
async def apply_now(request: Request, force: int = 0):
    """Push today's sound to every enabled device.

    Off the event loop: `reconcile` dials each door station in turn, so with
    several devices this is tens of seconds of blocking network I/O.
    """
    def _run() -> None:
        with session_scope() as db:
            reconcile(db, force=bool(force))

    await run_in_threadpool(_run)
    return RedirectResponse("/dashboard", status_code=303)
