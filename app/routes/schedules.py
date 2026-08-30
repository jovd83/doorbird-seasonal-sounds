"""Schedule CRUD for both kinds of schedule.

Chime schedules and auto-response schedules are the same record with a
different `kind`, so they share every helper here and differ only in the two
places it matters: which MP3s they may reference, and whether the wait
interval is editable. Two routers are built from the same code rather than
copied, so a fix to the date handling reaches both.
"""
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import holidays
from app.db import get_db, session_scope
from app.models import (
    KIND_AUTO_RESPONSE,
    KIND_CHIME,
    Device,
    Mp3Collection,
    Mp3File,
    Schedule,
)
from app.mp3_library import store_uploaded_mp3
from app.schedule_form import (
    MAX_DELAY_SECONDS,
    FormError,
    build_date_fields,
    build_day_rule,
    build_time_window,
    clean_delay,
    optional_int,
)
from app.scheduler import trigger_on_change
from app.security import require_auth, require_csrf
from app.shell import resolve_shell
from app.templating import templates


def _validated(fn, *args, **kwargs):
    """Run a `schedule_form` helper, turning its FormError into a 400.

    The rules themselves know nothing about HTTP -- that is the point of them
    living in their own module -- so the translation happens here, once.
    """
    try:
        return fn(*args, **kwargs)
    except FormError as exc:
        raise HTTPException(400, str(exc)) from exc


def _resolve_devices(db: Session, device_ids: list[int]) -> list[Device]:
    """Empty selection means every device, so it is stored as no rows."""
    if not device_ids:
        return []
    found = db.query(Device).filter(Device.id.in_(device_ids)).all()
    missing = set(device_ids) - {d.id for d in found}
    if missing:
        raise HTTPException(400, f"unknown device id(s): {sorted(missing)}")
    return found


def _require_mp3_of_kind(db: Session, mp3_id: int, kind: str) -> Mp3File:
    """A chime schedule may not point at an auto-response clip, or vice versa."""
    mp3 = db.get(Mp3File, mp3_id)
    if mp3 is None:
        raise HTTPException(400, "selected MP3 does not exist")
    if mp3.kind != kind:
        raise HTTPException(
            400,
            f"MP3 {mp3.label!r} is a {mp3.kind_label.lower()} clip; "
            f"pick one marked '{kind.replace('_', ' ')}' instead",
        )
    return mp3


def _require_collection_of_kind(db: Session, collection_id: int, kind: str) -> Mp3Collection:
    collection = db.get(Mp3Collection, collection_id)
    if collection is None:
        raise HTTPException(400, "selected collection does not exist")
    if collection.kind != kind:
        raise HTTPException(
            400,
            f"collection {collection.name!r} holds {collection.kind_label.lower()} clips; "
            f"pick one marked '{kind.replace('_', ' ')}' instead",
        )
    if not collection.mp3s:
        raise HTTPException(
            400, f"collection {collection.name!r} is empty — add some MP3s to it first")
    return collection


def _resolve_sound(
    db: Session,
    *,
    kind: str,
    mp3_id: int | None,
    collection_id: int | None,
    new_label: str | None,
    new_file: UploadFile | None,
) -> tuple[int, int | None]:
    """Work out (mp3_id, collection_id) for a schedule from the form's three
    mutually exclusive sound sources: a collection, an existing file, or an
    upload. A collection wins, and its first member becomes the stored single
    file so the schedule always has a concrete fallback.
    """
    if collection_id is not None:
        collection = _require_collection_of_kind(db, collection_id, kind)
        return collection.mp3s[0].id, collection.id
    return _resolve_or_upload_mp3(
        db, kind=kind, mp3_id=mp3_id, new_label=new_label, new_file=new_file), None


def _resolve_or_upload_mp3(
    db: Session,
    *,
    kind: str,
    mp3_id: int | None,
    new_label: str | None,
    new_file: UploadFile | None,
) -> int:
    if new_file is not None and new_file.filename:
        # Same storage path as the MP3 library page, including the size cap.
        # Uploaded from a schedule form, so it belongs to that form's slot and
        # is never the default.
        record = store_uploaded_mp3(
            db, upload=new_file, label=new_label or "", kind=kind, is_default=False)
        return record.id

    if mp3_id is None:
        raise HTTPException(
            400, "pick a collection or an existing MP3, or upload a new one")
    _require_mp3_of_kind(db, mp3_id, kind)
    return mp3_id


# --------------------------------------------------------------------------
# Router factory
# --------------------------------------------------------------------------


def _make_router(*, kind: str, prefix: str, page: dict) -> APIRouter:
    router = APIRouter(
        prefix=prefix,
        dependencies=[
            Depends(require_auth), Depends(require_csrf), Depends(resolve_shell)],
    )

    @router.get("")
    async def list_schedules(request: Request, db: Session = Depends(get_db)):
        schedules = (
            db.query(Schedule)
            .filter(Schedule.kind == kind)
            .order_by(Schedule.priority.desc(), Schedule.name)
            .all()
        )
        mp3s = (
            db.query(Mp3File)
            .filter(Mp3File.kind == kind)
            .order_by(Mp3File.label)
            .all()
        )
        collections = (
            db.query(Mp3Collection)
            .filter(Mp3Collection.kind == kind)
            .order_by(Mp3Collection.name)
            .all()
        )
        devices = db.query(Device).order_by(Device.name).all()
        return templates.TemplateResponse(
            request, "schedules.html",
            {
                "schedules": schedules,
                "mp3s": mp3s,
                "collections": collections,
                "devices": devices,
                "today": date.today(),
                "holiday_groups": holidays.grouped(),
                "holiday_total": len(holidays.HOLIDAYS),
                "day_presets": holidays.DAY_PRESETS,
                "weekday_labels": holidays.WEEKDAY_LABELS,
                "weekday_names": holidays.WEEKDAY_NAMES,
                "all_days_mask": holidays.ALL_DAYS,
                "kind": kind,
                "base_url": prefix,
                "max_delay": MAX_DELAY_SECONDS,
                **page,
            },
        )

    @router.post("/create")
    async def create_schedule(
        name: str = Form(...),
        start: str = Form(...),
        end: str = Form(""),
        recurring: bool = Form(False),
        mp3_id: str = Form(""),
        collection_id: str = Form(""),
        new_mp3_label: str = Form(""),
        new_mp3_file: UploadFile | None = File(None),
        priority: int = Form(100),
        enabled: bool = Form(True),
        # An unchecked HTML checkbox submits nothing at all, so the default has
        # to be False -- with Form(True) the box could never be turned off.
        all_day: bool = Form(False),
        start_time: str = Form(""),
        end_time: str = Form(""),
        delay_seconds: int = Form(0),
        device_ids: list[int] = Form(default=[]),
        weekdays: list[str] = Form(default=[]),
        holiday_keys: list[str] = Form(default=[]),
        skip_public_holidays: bool = Form(False),
        day_rule: bool = Form(False),
    ):
        fields = _validated(build_date_fields, start=start, end=end, recurring=recurring)
        fields |= _validated(
            build_time_window, all_day=all_day,
            start_time=start_time, end_time=end_time).as_fields()
        days = _validated(
            build_day_rule, weekdays=weekdays, holiday_keys=holiday_keys,
            skip_public_holidays=skip_public_holidays, present=day_rule)
        fields |= days.as_fields()
        with session_scope() as db:
            if db.query(Schedule).filter(Schedule.name == name).first():
                raise HTTPException(400, f"Schedule {name!r} already exists")
            resolved_mp3, resolved_collection = _resolve_sound(
                db, kind=kind,
                mp3_id=_validated(optional_int, mp3_id, "MP3"),
                collection_id=_validated(optional_int, collection_id, "collection"),
                new_label=new_mp3_label, new_file=new_mp3_file,
            )
            schedule = Schedule(
                name=name.strip(),
                kind=kind,
                mp3_id=resolved_mp3,
                collection_id=resolved_collection,
                priority=priority,
                enabled=enabled,
                delay_seconds=_validated(clean_delay, kind, delay_seconds,
                                         auto_response_kind=KIND_AUTO_RESPONSE),
                **fields,
            )
            schedule.devices = _resolve_devices(db, device_ids)
            schedule.set_holiday_keys(days.holiday_keys)
            db.add(schedule)
        trigger_on_change()
        return RedirectResponse(prefix, status_code=303)

    @router.post("/{schedule_id}/update")
    async def update_schedule(
        schedule_id: int,
        name: str = Form(...),
        mp3_id: str = Form(""),
        collection_id: str = Form(""),
        start: str = Form(...),
        end: str = Form(""),
        recurring: bool = Form(False),
        priority: int = Form(100),
        enabled: bool = Form(True),
        # An unchecked HTML checkbox submits nothing at all, so the default has
        # to be False -- with Form(True) the box could never be turned off.
        all_day: bool = Form(False),
        start_time: str = Form(""),
        end_time: str = Form(""),
        delay_seconds: int = Form(0),
        device_ids: list[int] = Form(default=[]),
        weekdays: list[str] = Form(default=[]),
        holiday_keys: list[str] = Form(default=[]),
        skip_public_holidays: bool = Form(False),
        day_rule: bool = Form(False),
    ):
        fields = _validated(build_date_fields, start=start, end=end, recurring=recurring)
        fields |= _validated(
            build_time_window, all_day=all_day,
            start_time=start_time, end_time=end_time).as_fields()
        days = _validated(
            build_day_rule, weekdays=weekdays, holiday_keys=holiday_keys,
            skip_public_holidays=skip_public_holidays, present=day_rule)
        fields |= days.as_fields()
        with session_scope() as db:
            s = db.get(Schedule, schedule_id)
            if not s or s.kind != kind:
                raise HTTPException(404, "schedule not found")
            resolved_mp3, resolved_collection = _resolve_sound(
                db, kind=kind,
                mp3_id=_validated(optional_int, mp3_id, "MP3"),
                collection_id=_validated(optional_int, collection_id, "collection"),
                new_label=None, new_file=None,
            )
            s.name = name.strip()
            s.mp3_id = resolved_mp3
            s.collection_id = resolved_collection
            s.priority = priority
            s.enabled = enabled
            s.delay_seconds = _validated(clean_delay, kind, delay_seconds,
                                         auto_response_kind=KIND_AUTO_RESPONSE)
            s.devices = _resolve_devices(db, device_ids)
            s.set_holiday_keys(days.holiday_keys)
            for k, v in fields.items():
                setattr(s, k, v)
        trigger_on_change()
        return RedirectResponse(prefix, status_code=303)

    @router.post("/{schedule_id}/delete")
    async def delete_schedule(schedule_id: int):
        with session_scope() as db:
            s = db.get(Schedule, schedule_id)
            if s and s.kind == kind:
                db.delete(s)
        trigger_on_change()
        return RedirectResponse(prefix, status_code=303)

    return router


router = _make_router(
    kind=KIND_CHIME,
    prefix="/schedules",
    page={
        "page_title": "Chime schedules",
        "noun": "chime schedule",
        "intro": (
            "The highest-priority chime schedule matching <em>right now</em> decides "
            "what the doorbell sounds like. If two match at the same priority, the "
            "more specific one wins — narrower time window first, then narrower date "
            "range. When nothing matches, the default MP3 is used."
        ),
    },
)

auto_response_router = _make_router(
    kind=KIND_AUTO_RESPONSE,
    prefix="/auto-responses",
    page={
        "page_title": "Auto-response schedules",
        "noun": "auto response",
        "intro": (
            "An auto response is a spoken message played out of the door speaker "
            "<em>after</em> the chime — “you can leave the parcel on the porch”. "
            "Set how long to wait once the chime has finished. Unlike chimes there "
            "is no fallback: when no auto-response schedule matches, nothing is said."
        ),
    },
)
