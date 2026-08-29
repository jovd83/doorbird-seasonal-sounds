import csv
import io
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db, session_scope
from app.models import AuditLog, Device, Mp3File, Schedule
from app.security import require_auth, require_csrf
from app.shell import resolve_shell
from app.templating import templates
from app.timezone import now_local

router = APIRouter(
    prefix="/audit",
    dependencies=[Depends(require_auth), Depends(require_csrf), Depends(resolve_shell)],
)


def _lookups(db: Session) -> tuple[dict, dict, dict]:
    return (
        {d.id: d for d in db.query(Device).all()},
        {m.id: m for m in db.query(Mp3File).all()},
        {s.id: s for s in db.query(Schedule).all()},
    )


@router.get("")
async def list_audit(request: Request, db: Session = Depends(get_db), limit: int = 200):
    rows = (
        db.query(AuditLog)
        .order_by(AuditLog.ts.desc())
        .limit(limit)
        .all()
    )
    devices, mp3s, scheds = _lookups(db)
    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "rows": rows,
            "devices": devices,
            "mp3s": mp3s,
            "scheds": scheds,
            "limit": limit,
            "total": db.query(AuditLog).count(),
        },
    )


@router.get("/download")
async def download_audit():
    """The whole log as CSV, oldest first.

    Deliberately not limited by the page's `limit`: the point of downloading
    is to keep what the page is about to drop off the end -- or what
    "Clear log" is about to delete.

    Genuinely streamed. The previous version built the entire CSV in a
    `StringIO` and handed it over as `iter([buffer.getvalue()])`, so the
    `StreamingResponse` was decorative and peak memory was the whole export --
    `yield_per` only paced the read from SQLite, not the response.
    """
    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        _csv_rows(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="doorbird-audit-{stamp}.csv"'},
    )


def _csv_rows() -> Iterator[str]:
    """Yield the export one line at a time, holding its own session.

    A dependency-injected session would be closed when the handler returns,
    which for a streaming response is *before* the body has been produced.
    """
    line = io.StringIO()
    writer = csv.writer(line, lineterminator="\n")

    def emit(row: list[str]) -> str:
        line.seek(0)
        line.truncate(0)
        writer.writerow(row)
        return line.getvalue()

    with session_scope() as db:
        devices, mp3s, scheds = _lookups(db)
        yield emit(["timestamp", "action", "device", "mp3", "schedule", "success", "message"])
        for r in db.query(AuditLog).order_by(AuditLog.ts.asc()).yield_per(500):
            device = devices.get(r.device_id)
            mp3 = mp3s.get(r.mp3_id)
            sched = scheds.get(r.schedule_id)
            yield emit([
                r.ts.strftime("%Y-%m-%d %H:%M:%S") if r.ts else "",
                r.action,
                device.name if device else "",
                mp3.label if mp3 else "",
                sched.name if sched else "",
                "yes" if r.success else "no",
                r.message or "",
            ])


@router.post("/clear")
async def clear_audit():
    """Wipe the log, then record the wipe so the page is never blank-and-silent."""
    with session_scope() as db:
        removed = db.query(AuditLog).delete()
        db.add(AuditLog(
            action="audit-clear",
            success=True,
            message=f"audit log cleared ({removed} entries removed)",
        ))
    return RedirectResponse(
        f"/audit?msg=Audit+log+cleared+%28{removed}+entries+removed%29", status_code=303)
