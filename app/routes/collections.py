"""Collections: named bags of interchangeable sounds.

A schedule pointing at a collection draws a different member on each ring, so
"Christmas" can be three chimes rather than the same one for six weeks. The
draw itself lives in `app.engine.pick_sound`; this module is only the CRUD.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db, session_scope
from app.models import KIND_CHIME, KINDS, Mp3Collection, Mp3File, Schedule
from app.scheduler import trigger_on_change
from app.security import require_auth, require_csrf
from app.shell import resolve_shell
from app.templating import templates

router = APIRouter(
    prefix="/collections",
    dependencies=[Depends(require_auth), Depends(require_csrf), Depends(resolve_shell)],
)


@router.get("")
async def list_collections(request: Request, db: Session = Depends(get_db)):
    collections = db.query(Mp3Collection).order_by(Mp3Collection.kind, Mp3Collection.name).all()
    mp3s = db.query(Mp3File).order_by(Mp3File.kind, Mp3File.label).all()
    # Which schedules depend on each collection, so the page can say why a
    # delete is refused before the user tries it.
    users: dict[int, list[str]] = {}
    for s in db.query(Schedule).filter(Schedule.collection_id.isnot(None)).all():
        users.setdefault(s.collection_id, []).append(s.name)
    return templates.TemplateResponse(
        request,
        "collections.html",
        {"collections": collections, "mp3s": mp3s, "kinds": KINDS, "users": users},
    )


def _members_of_kind(db: Session, mp3_ids: list[int], kind: str) -> list[Mp3File]:
    if not mp3_ids:
        return []
    found = db.query(Mp3File).filter(Mp3File.id.in_(mp3_ids)).all()
    missing = set(mp3_ids) - {m.id for m in found}
    if missing:
        raise HTTPException(400, f"unknown MP3 id(s): {sorted(missing)}")
    wrong = [m.label for m in found if m.kind != kind]
    if wrong:
        raise HTTPException(
            400,
            f"a {kind.replace('_', ' ')} collection cannot contain "
            f"{', '.join(sorted(wrong))} — change the MP3's type first",
        )
    return found


@router.post("/create")
async def create_collection(
    name: str = Form(...),
    kind: str = Form(KIND_CHIME),
    mp3_ids: list[int] = Form(default=[]),
):
    if kind not in KINDS:
        raise HTTPException(400, f"unknown collection type {kind!r}")
    label = name.strip()
    if not label:
        raise HTTPException(400, "give the collection a name")
    with session_scope() as db:
        if db.query(Mp3Collection).filter(Mp3Collection.name == label).first():
            raise HTTPException(400, f"collection {label!r} already exists")
        collection = Mp3Collection(name=label, kind=kind)
        collection.mp3s = _members_of_kind(db, mp3_ids, kind)
        db.add(collection)
    return RedirectResponse("/collections", status_code=303)


@router.post("/{collection_id}/update")
async def update_collection(
    collection_id: int,
    name: str = Form(...),
    mp3_ids: list[int] = Form(default=[]),
):
    label = name.strip()
    if not label:
        raise HTTPException(400, "give the collection a name")
    with session_scope() as db:
        c = db.get(Mp3Collection, collection_id)
        if not c:
            raise HTTPException(404, "collection not found")
        clash = (
            db.query(Mp3Collection)
            .filter(Mp3Collection.name == label, Mp3Collection.id != collection_id)
            .first()
        )
        if clash:
            raise HTTPException(400, f"collection {label!r} already exists")
        members = _members_of_kind(db, mp3_ids, c.kind)
        # An empty collection would leave its schedules silently falling back
        # to their stored single file, which is not what the page shows.
        if not members and db.query(Schedule).filter(
                Schedule.collection_id == collection_id).first():
            raise HTTPException(
                400, "this collection is in use by a schedule; it needs at least one MP3")
        c.name = label
        c.mp3s = members
        # Keep each schedule's stored single file pointing at a real member,
        # so a collection that later empties still has something to fall back on.
        if members:
            member_ids = {m.id for m in members}
            for s in db.query(Schedule).filter(Schedule.collection_id == collection_id).all():
                if s.mp3_id not in member_ids:
                    s.mp3_id = members[0].id
    trigger_on_change()
    return RedirectResponse("/collections", status_code=303)


@router.post("/{collection_id}/delete")
async def delete_collection(collection_id: int):
    with session_scope() as db:
        c = db.get(Mp3Collection, collection_id)
        if not c:
            return RedirectResponse("/collections", status_code=303)
        in_use = db.query(Schedule).filter(Schedule.collection_id == collection_id).first()
        if in_use:
            raise HTTPException(
                400,
                f"{c.name!r} is used by schedule {in_use.name!r}; "
                "point that schedule at a single MP3 first",
            )
        db.delete(c)
    return RedirectResponse("/collections", status_code=303)
