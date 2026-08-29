"""Reliability behaviours that are invisible until they fail in production.

WAL journalling, real CSV streaming, audit retention, the fire-and-forget
relay task, and forward-URL validation. Each one covers something that either
degraded silently or only showed up under load.
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import text

from app import settings_store
from app.db import engine, init_db, session_scope
from app.models import AuditLog, prune_audit_log
from app.ring import playback
from app.timezone import now_local

# ------------------------------------------------------------------- SQLite


def test_sqlite_runs_in_wal_mode():
    """Under the default `delete` journal a reader blocks a writer outright.

    This database is written by the request threadpool, a watcher per device, a
    chime thread per ring, an auto-response thread per ring, and APScheduler.
    """
    init_db()
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"


def test_sqlite_waits_rather_than_failing_on_a_lock():
    init_db()
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() >= 30000


def test_concurrent_writers_do_not_trip_over_each_other():
    """The shape that produced 'database is locked' on the NAS."""
    init_db()
    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            for i in range(20):
                with session_scope() as db:
                    db.add(AuditLog(action=f"load-{n}", success=True, message=str(i)))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"concurrent writes failed: {errors[:3]}"

    with session_scope() as db:
        written = db.query(AuditLog).filter(AuditLog.action.like("load-%")).count()
        assert written == 160
        db.query(AuditLog).filter(AuditLog.action.like("load-%")).delete(
            synchronize_session=False)


# ---------------------------------------------------------- audit retention


def test_pruning_removes_only_rows_past_the_window():
    from datetime import timedelta

    init_db()
    now = now_local()
    with session_scope() as db:
        db.query(AuditLog).delete()
        db.add(AuditLog(action="ancient", success=True, ts=now - timedelta(days=400)))
        db.add(AuditLog(action="old", success=True, ts=now - timedelta(days=100)))
        db.add(AuditLog(action="recent", success=True, ts=now - timedelta(days=1)))

    with session_scope() as db:
        removed = prune_audit_log(db, older_than_days=365, now=now)
    assert removed == 1

    with session_scope() as db:
        remaining = {r.action for r in db.query(AuditLog).all()}
    assert remaining == {"old", "recent"}


def test_pruning_is_disabled_by_a_zero_window():
    """The escape hatch for anyone who wants to keep everything."""
    init_db()
    with session_scope() as db:
        db.query(AuditLog).delete()
        db.add(AuditLog(action="keep", success=True))
    with session_scope() as db:
        assert prune_audit_log(db, older_than_days=0) == 0
    with session_scope() as db:
        assert db.query(AuditLog).count() == 1


# ------------------------------------------------------------ forward URL


@pytest.mark.parametrize("bad", [
    "file:///etc/passwd",
    "ftp://example.test/x",
    "not-a-url",
    "example.test/ring",       # no scheme
    "http://",                 # no host
])
def test_a_bad_forward_url_is_refused(bad):
    with pytest.raises(ValueError):
        settings_store.validate_forward_url(bad)


@pytest.mark.parametrize("good", [
    "http://10.0.0.7/api/ring",
    "https://hass.local:8123/api/webhook/abc",
])
def test_a_good_forward_url_is_kept(good):
    assert settings_store.validate_forward_url(good) == good


def test_an_empty_forward_url_means_disabled():
    assert settings_store.validate_forward_url("") == ""
    assert settings_store.validate_forward_url("   ") == ""


# ------------------------------------------------------ fire-and-forget relay


@pytest.mark.asyncio
async def test_the_relay_task_is_held_until_it_finishes():
    """`asyncio.create_task` alone leaves only a weak reference.

    A task nobody keeps can be collected before it ever runs — rarely, under
    load, and impossible to reproduce on demand.
    """
    import asyncio

    from app.routes import ring_hook

    ran = asyncio.Event()

    async def _fake_forward(url: str) -> None:
        await asyncio.sleep(0)
        ran.set()

    original = ring_hook._forward
    ring_hook._forward = _fake_forward
    try:
        task = ring_hook._spawn_forward("http://example.test/x")
        assert task in ring_hook._pending_forwards, "the task was not retained"
        await asyncio.wait_for(ran.wait(), timeout=2)
        await task
        # done-callbacks are dispatched via call_soon, so give the loop one
        # more turn before checking that the set was drained.
        await asyncio.sleep(0)
        assert task not in ring_hook._pending_forwards, "the task was never released"
    finally:
        ring_hook._forward = original


# ------------------------------------------------------ auto-response waiting


def test_a_pending_auto_response_can_be_cancelled():
    """It used to be `time.sleep(delay)` — up to an hour, uninterruptible."""
    cancel = threading.Event()
    done: list[bool] = []

    def waiter() -> None:
        done.append(playback._wait_out_delay(30, cancel))

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    cancel.set()
    t.join(timeout=5)

    assert not t.is_alive(), "the wait ignored its cancel flag"
    assert done == [True]


def test_a_wait_that_is_not_cancelled_reports_so():
    assert playback._wait_out_delay(0, threading.Event()) is False
