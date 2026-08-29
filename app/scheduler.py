import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import session_scope
from app.engine import reconcile

log = logging.getLogger("doorbird.scheduler")
_scheduler: BackgroundScheduler | None = None


def _prewarm_today() -> None:
    """Transcode today's MP3 ahead of time so the first ring isn't delayed.

    In ring-chime mode there is nothing to push to the device, so the daily
    job's useful work is making sure the mu-law rendering of whatever is
    active today is already sitting in the cache.
    """
    from app.audio import ensure_ulaw
    from app.engine import mp3_path, today_resolution

    with session_scope() as db:
        res = today_resolution(db)
        source = mp3_path(res.mp3) if res else None
        label = res.mp3.label if res else None

    if source is None:
        log.warning("daily prewarm: no default MP3 set; nothing to prepare")
        return
    try:
        ensure_ulaw(source)
        log.info("daily prewarm: %r ready for the door speaker", label)
    except Exception as exc:
        log.warning("daily prewarm failed for %r: %s", label, exc)


def _prune_audit_log() -> None:
    """Drop audit rows past the retention window.

    Nothing removed them before, so on a busy front door the table grew without
    limit and the audit page counted all of it on every load.
    """
    from app.models import prune_audit_log

    days = settings.audit_retention_days
    if days <= 0:
        return
    try:
        with session_scope() as db:
            removed = prune_audit_log(db, older_than_days=days)
        if removed:
            log.info("audit retention: removed %d row(s) older than %d days", removed, days)
    except Exception:
        log.exception("audit retention pass failed")


def _daily_work() -> None:
    """Ring-chime mode has nothing to push, so only reconcile when asked to."""
    if settings.button_sound_upload_enabled:
        with session_scope() as db:
            log.info("daily reconcile done: %s", reconcile(db))
    if settings.ring_chime_enabled:
        _prewarm_today()
    _prune_audit_log()


def _run_daily() -> None:
    log.info("daily run starting at %s", datetime.now().isoformat())
    _daily_work()


def _run_on_change() -> None:
    log.info("on-change run triggered")
    _daily_work()


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    tz = ZoneInfo(settings.timezone)
    _scheduler = BackgroundScheduler(timezone=tz)
    _scheduler.add_job(
        _run_daily,
        CronTrigger(hour=settings.daily_run_hour, minute=settings.daily_run_minute, timezone=tz),
        id="daily",
        replace_existing=True,
    )
    _scheduler.start()
    log.info("scheduler started: daily run at %02d:%02d %s",
             settings.daily_run_hour, settings.daily_run_minute, settings.timezone)


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def trigger_on_change() -> None:
    if _scheduler is None:
        return
    _scheduler.add_job(_run_on_change, id="on-change", replace_existing=True)
