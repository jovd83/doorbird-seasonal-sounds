"""The persisted last-ring stamp behind the dashboard's headline figure.

It used to live on the watcher thread object, which meant it was wiped by every
restart while the audit log still listed the rings, and a ring arriving through
the webhook never reached a watcher so it never registered at all. These pin
the replacement: one stamp on the device row, written wherever a ring is
claimed.
"""
from __future__ import annotations

import pytest

# Environment isolation lives in conftest.py so it is applied before any import.
from app.crypto import encrypt
from app.db import init_db, session_scope
from app.models import Device
from app.ring import debounce


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()
    yield


@pytest.fixture(autouse=True)
def _isolate():
    debounce._last_fire.clear()
    yield
    debounce._last_fire.clear()


def _device(name: str) -> int:
    with session_scope() as db:
        d = Device(
            name=name, host="192.0.2.10", username="u",
            password_enc=encrypt("p"), enabled=True,
        )
        db.add(d)
        db.flush()
        return d.id


def _last_ring(device_id: int):
    with session_scope() as db:
        return db.get(Device, device_id).last_ring_at


def test_a_claimed_ring_is_stamped_on_the_device():
    device_id = _device("stamp-claimed")
    assert _last_ring(device_id) is None

    assert debounce.claim_ring(device_id) is True
    assert _last_ring(device_id) is not None


def test_a_debounced_duplicate_does_not_move_the_stamp():
    """The second report of one press is the same press, not a later ring."""
    device_id = _device("stamp-debounced")
    assert debounce.claim_ring(device_id) is True
    first = _last_ring(device_id)

    assert debounce.claim_ring(device_id) is False
    assert _last_ring(device_id) == first


def test_the_stamp_outlives_the_watcher():
    """The whole point: it is device state, readable with no thread running."""
    device_id = _device("stamp-survives")
    debounce.claim_ring(device_id)

    # No watcher exists for this device -- as after a restart, or on an
    # install triggered purely by the webhook.
    from app.ring import watcher
    assert not [w for w in watcher.status() if w.device_id == device_id]
    assert _last_ring(device_id) is not None


def test_a_database_failure_still_lets_the_chime_through(monkeypatch):
    """Bookkeeping must never cost a ring that was already claimed."""
    device_id = _device("stamp-db-down")

    def _boom(*a, **k):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(debounce, "_record_ring", _boom)
    with pytest.raises(RuntimeError):
        debounce.claim_ring(device_id)

    # And with the real guard in place, the exception is swallowed instead.
    monkeypatch.undo()
    monkeypatch.setattr("app.db.session_scope", _boom)
    debounce._last_fire.clear()
    assert debounce.claim_ring(device_id) is True
