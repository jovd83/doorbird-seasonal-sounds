"""The MP3 library page: upload, retype, promote, delete.

Storing an upload lives in `app.mp3_library` because the schedule forms can
upload too, and the two copies of that sequence had already drifted.
"""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db import get_db, session_scope
from app.models import KIND_CHIME, KINDS, Mp3Collection, Mp3File, Schedule
from app.mp3_library import delete_stored_file, make_default, store_uploaded_mp3
from app.scheduler import trigger_on_change
from app.security import require_auth, require_csrf
from app.shell import resolve_shell
from app.templating import templates

router = APIRouter(
    prefix="/mp3s",
    dependencies=[Depends(require_auth), Depends(require_csrf), Depends(resolve_shell)],
)


def _valid_kind(kind: str) -> str:
    if kind not in KINDS:
        raise HTTPException(400, f"unknown MP3 type {kind!r}")
    return kind


def _collections_holding(db: Session, mp3_id: int) -> list[Mp3Collection]:
    return (
        db.query(Mp3Collection)
        .filter(Mp3Collection.mp3s.any(Mp3File.id == mp3_id))
        .all()
    )


@router.get("")
async def list_mp3s(request: Request, db: Session = Depends(get_db)):
    # Grouped by kind so the two libraries read as two lists, not one mixed
    # one -- picking a chime out of a list of spoken messages is error-prone.
    items = db.query(Mp3File).order_by(Mp3File.kind, Mp3File.label).all()
    return templates.TemplateResponse(
        request, "mp3s.html",
        {"items": items, "kinds": KINDS, "max_upload_mb": settings.max_upload_bytes // 1024 // 1024},
    )


@router.get("/{mp3_id}/file")
async def stream_mp3(mp3_id: int, db: Session = Depends(get_db)):
    m = db.get(Mp3File, mp3_id)
    if not m:
        raise HTTPException(404, "mp3 not found")
    path = settings.mp3_dir / m.filename
    if not path.exists():
        raise HTTPException(404, "mp3 file missing on disk")
    return FileResponse(path, media_type="audio/mpeg", filename=f"{m.label}.mp3")


@router.post("/upload")
async def upload_mp3(
    label: str = Form(...),
    kind: str = Form(KIND_CHIME),
    is_default: bool = Form(False),
    file: UploadFile = File(...),
):
    def _store() -> list[str]:
        with session_scope() as db:
            record = store_uploaded_mp3(
                db, upload=file, label=label, kind=kind, is_default=is_default)
            # Read the validator's advisory notes back off the stored row so
            # the redirect can surface them without a second inspection.
            return _advisories(record)

    # Off the event loop: writing to a NAS bind mount and parsing MP3 headers
    # are both blocking, and this is one event loop for the whole app.
    issues = await run_in_threadpool(_store)

    trigger_on_change()
    suffix = "?warn=" + ",".join(issues) if issues else ""
    return RedirectResponse(f"/mp3s{suffix}", status_code=303)


def _advisories(record: Mp3File) -> list[str]:
    """Non-fatal things worth telling the user about a stored file.

    Distinct from the hard limits in `mp3_library`, which reject outright.
    """
    from app.mp3_validator import (
        DOORBIRD_ALLOWED_SAMPLE_RATES,
        DOORBIRD_MAX_DURATION_S,
    )

    notes: list[str] = []
    if record.sample_rate_hz not in DOORBIRD_ALLOWED_SAMPLE_RATES:
        notes.append(
            f"sample_rate={record.sample_rate_hz}Hz (DoorBird requires 44100 or 48000)")
    if (record.duration_seconds or 0) > DOORBIRD_MAX_DURATION_S:
        notes.append(
            f"duration={record.duration_seconds:.2f}s "
            f"(DoorBird allows max {DOORBIRD_MAX_DURATION_S:.0f}s)")
    return notes


@router.post("/{mp3_id}/default")
async def set_default(mp3_id: int):
    with session_scope() as db:
        target = db.get(Mp3File, mp3_id)
        if not target:
            raise HTTPException(404, "mp3 not found")
        make_default(db, target)
    trigger_on_change()
    return RedirectResponse("/mp3s", status_code=303)


@router.post("/{mp3_id}/kind")
async def set_kind(mp3_id: int, kind: str = Form(...)):
    """Move a clip between the chime and auto-response libraries.

    Refused while a schedule of the old kind still points at it: the schedule
    would silently start playing a sound meant for the other slot.
    """
    kind = _valid_kind(kind)
    with session_scope() as db:
        target = db.get(Mp3File, mp3_id)
        if not target:
            raise HTTPException(404, "mp3 not found")
        if target.kind == kind:
            return RedirectResponse("/mp3s", status_code=303)
        in_use = db.query(Schedule).filter(Schedule.mp3_id == mp3_id).first()
        if in_use:
            raise HTTPException(
                400,
                f"{target.label!r} is used by schedule {in_use.name!r}; "
                "point that schedule at another MP3 first",
            )
        held = _collections_holding(db, mp3_id)
        if held:
            names = ", ".join(sorted(c.name for c in held))
            raise HTTPException(
                400,
                f"{target.label!r} belongs to collection(s) {names}; "
                "a collection only holds MP3s of its own type, so remove it there first",
            )
        if target.is_default and kind != KIND_CHIME:
            raise HTTPException(400, "this is the default chime; pick a new default first")
        target.kind = kind
    trigger_on_change()
    return RedirectResponse("/mp3s", status_code=303)


@router.post("/{mp3_id}/delete")
async def delete_mp3(mp3_id: int):
    with session_scope() as db:
        m = db.get(Mp3File, mp3_id)
        if not m:
            return RedirectResponse("/mp3s", status_code=303)
        if db.query(Schedule).filter(Schedule.mp3_id == mp3_id).first():
            raise HTTPException(400, "MP3 is in use by a schedule; delete the schedule first")
        held = _collections_holding(db, mp3_id)
        if held:
            names = ", ".join(sorted(c.name for c in held))
            raise HTTPException(
                400, f"MP3 belongs to collection(s) {names}; remove it from them first")
        if m.is_default:
            raise HTTPException(400, "MP3 is marked default; pick a new default first")
        delete_stored_file(m)
        db.delete(m)
    return RedirectResponse("/mp3s", status_code=303)
