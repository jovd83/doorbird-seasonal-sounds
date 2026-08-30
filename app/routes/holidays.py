"""The holiday reference page.

Read-only on purpose: the nineteen entries are a catalogue in code, not user
data. What this page is actually for is answering "when is that, then?" — the
five holidays that move with Easter have no date you can read off a rule, and
before this page the only way to find out was to trust the picker.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app import holiday_store, holidays
from app.db import get_db
from app.models import Schedule, ScheduleHoliday
from app.security import require_auth, require_csrf
from app.shell import resolve_shell
from app.templating import templates
from app.timezone import now_local

router = APIRouter(
    dependencies=[Depends(require_auth), Depends(require_csrf), Depends(resolve_shell)],
)


@router.get("/holidays")
async def holiday_list(request: Request, db: Session = Depends(get_db)):
    today = now_local().date()
    next_dates = holiday_store.next_dates(db, today)

    # Which schedules point at each holiday, so the page can say whether an
    # entry is actually in use rather than just listing all nineteen.
    used: dict[str, list[str]] = {}
    rows = (
        db.query(ScheduleHoliday.holiday_key, Schedule.name)
        .join(Schedule, Schedule.id == ScheduleHoliday.schedule_id)
        .order_by(Schedule.name)
        .all()
    )
    for key, name in rows:
        used.setdefault(key, []).append(name)

    return templates.TemplateResponse(
        request,
        "holidays.html",
        {
            "groups": holidays.grouped(),
            "total": len(holidays.HOLIDAYS),
            "next_dates": next_dates,
            "used": used,
            "today": today,
            "moving_keys": holidays.MOVING_KEYS,
            "stored_span": holiday_store.stored_span(db),
            "horizon_years": holidays.HORIZON_YEARS,
            "stored_rows": len(holidays.MOVING) * holidays.HORIZON_YEARS,
        },
    )
