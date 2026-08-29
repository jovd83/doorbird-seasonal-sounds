"""What a real ring does, with the door station stubbed out.

The unit tests pin the draw and the resolution separately; this file follows a
whole button press through `play_active_chime` — the collection rotating, the
auto response following it, and the audit trail both leave behind.
"""
from __future__ import annotations

import itertools
import random
import threading

import pytest

# Environment isolation lives in conftest.py so it is applied before any import.
from app import engine, ring, settings_store
from app.crypto import encrypt
from app.db import init_db, session_scope
from app.models import (
    KIND_AUTO_RESPONSE,
    KIND_CHIME,
    AuditLog,
    Device,
    Mp3Collection,
    Mp3File,
    Schedule,
)
from app.ring import debounce, playback


class _FakeDoor:
    """Stands in for DoorBirdClient; records nothing but succeeds."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def play_ulaw(self, path):
        return "played"


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()
    yield


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _settle_auto_responses()
    debounce._last_fire.clear()
    engine._last_pick.clear()
    # A fixed seed, or the collection assertions are genuinely flaky: with
    # three members and six draws, "every member was used" fails about 6% of
    # the time purely by chance.
    monkeypatch.setattr(engine, "_rng", random.Random(20260823))
    settings_store.set_play_default_when_idle(True)
    # Never touch ffmpeg or the network from a test.
    monkeypatch.setattr(playback, "ensure_ulaw", lambda p: p)
    monkeypatch.setattr(playback, "DoorBirdClient", lambda creds: _FakeDoor())
    yield
    # An auto response left waiting would wake during the *next* test, after
    # `_fresh_world` has wiped the tables, and write a stray audit row there.
    _settle_auto_responses()
    debounce._last_fire.clear()
    engine._last_pick.clear()


def _settle_auto_responses() -> None:
    """Cancel every pending auto response and wait for its thread to finish."""
    ring.cancel_pending_auto_responses()
    for t in threading.enumerate():
        if t.name.startswith("auto-response-"):
            t.join(timeout=5)


def _fresh_world(*, with_collection: bool, with_auto_response: bool, delay: int = 0) -> int:
    """Build a device, a library and the schedules, returning the device id."""
    with session_scope() as db:
        db.query(AuditLog).delete()
        db.query(Schedule).delete()
        for c in db.query(Mp3Collection).all():
            c.mp3s = []
            db.delete(c)
        db.query(Mp3File).delete()
        db.query(Device).delete()
        db.flush()

        device = Device(name="RING", host="10.0.0.1", username="u",
                        password_enc=encrypt("p"), enabled=True)
        db.add(device)

        default = Mp3File(label="Default", filename="default.mp3", size_bytes=1,
                          kind=KIND_CHIME, is_default=True)
        members = [
            Mp3File(label=f"Xmas {n}", filename=f"x{n}.mp3", size_bytes=1, kind=KIND_CHIME)
            for n in (1, 2, 3)
        ]
        db.add_all([default, *members])
        db.flush()

        schedule = Schedule(
            name="Christmas", kind=KIND_CHIME, mp3_id=members[0].id,
            start_month=1, start_day=1, end_month=12, end_day=31,
            priority=100, enabled=True,
        )
        if with_collection:
            collection = Mp3Collection(name="Christmas set", kind=KIND_CHIME)
            collection.mp3s = members
            db.add(collection)
            db.flush()
            schedule.collection_id = collection.id
        db.add(schedule)

        if with_auto_response:
            spoken = Mp3File(label="Porch", filename="porch.mp3", size_bytes=1,
                             kind=KIND_AUTO_RESPONSE)
            db.add(spoken)
            db.flush()
            db.add(Schedule(
                name="Parcel", kind=KIND_AUTO_RESPONSE, mp3_id=spoken.id,
                start_month=1, start_day=1, end_month=12, end_day=31,
                priority=100, enabled=True, delay_seconds=delay,
            ))

        db.flush()
        return device.id


def _chime_rows() -> list[AuditLog]:
    with session_scope() as db:
        return [
            (r.action, r.mp3_id, r.success)
            for r in db.query(AuditLog).order_by(AuditLog.id).all()
        ]


def _ring(device_id: int, times: int = 1) -> None:
    for _ in range(times):
        debounce._last_fire.clear()      # each call is a separate press
        ring.play_active_chime(device_id)
    # The auto response runs on its own thread; wait for it to finish.
    for t in threading.enumerate():
        if t.name.startswith("auto-response-"):
            t.join(timeout=5)


# --- collections rotate ---------------------------------------------------

def test_consecutive_rings_play_different_members(monkeypatch):
    device_id = _fresh_world(with_collection=True, with_auto_response=False)
    _ring(device_id, times=6)

    played = [mp3_id for action, mp3_id, _ in _chime_rows() if action == "chime"]
    assert len(played) == 6
    assert len(set(played)) == 3, "all three members should get used"
    assert all(a != b for a, b in itertools.pairwise(played)), \
        "the same chime must never play twice running"


def test_a_schedule_without_a_collection_always_plays_its_file(monkeypatch):
    device_id = _fresh_world(with_collection=False, with_auto_response=False)
    _ring(device_id, times=3)
    played = {mp3_id for action, mp3_id, _ in _chime_rows() if action == "chime"}
    assert len(played) == 1


# --- auto responses follow the chime -------------------------------------

def test_a_ring_produces_a_chime_then_an_auto_response():
    device_id = _fresh_world(with_collection=False, with_auto_response=True)
    _ring(device_id)

    actions = [action for action, _, _ in _chime_rows()]
    assert actions == ["chime", "auto-response"], actions
    assert all(ok for _, _, ok in _chime_rows())


def test_the_auto_response_waits_its_interval(monkeypatch):
    device_id = _fresh_world(with_collection=False, with_auto_response=True, delay=7)
    slept: list[float] = []
    monkeypatch.setattr(
        playback, "_wait_out_delay",
        lambda delay, cancel: (slept.append(delay), False)[1])

    _ring(device_id)
    assert slept == [7], "the schedule's wait interval should be honoured once"


def test_no_auto_response_schedule_means_no_second_sound():
    device_id = _fresh_world(with_collection=False, with_auto_response=False)
    _ring(device_id)
    assert [action for action, _, _ in _chime_rows()] == ["chime"]


def test_a_silent_chime_still_speaks_its_auto_response():
    """The two are independent: no chime does not mean no message."""
    device_id = _fresh_world(with_collection=False, with_auto_response=True)
    with session_scope() as db:
        # Drop the chime schedule so nothing matches, and turn the fallback off.
        db.query(Schedule).filter(Schedule.kind == KIND_CHIME).delete()
        db.query(AuditLog).delete()
    settings_store.set_play_default_when_idle(False)

    _ring(device_id)

    actions = [action for action, _, _ in _chime_rows()]
    assert "auto-response" in actions
    chime = [r for r in _chime_rows() if r[0] == "chime"]
    assert chime and chime[0][1] is None, "no chime audio should have been sent"
