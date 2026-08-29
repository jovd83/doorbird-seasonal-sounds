"""Watcher lifecycle: does a device edit actually reach the listener thread?

Before the fingerprint check, it did not. `update_device` saved the row and
called `refresh()`, but `refresh()` only stopped watchers for *disabled*
devices and `start()` skipped any device that already had a live thread — so a
changed host, username or password kept dialling the old address until someone
restarted the container, with a saved form and a green listener badge saying
everything was fine.
"""
from __future__ import annotations

import threading

import pytest

from app import ring
from app.crypto import encrypt
from app.db import init_db, session_scope
from app.doorbird import DeviceCreds
from app.models import Device
from app.ring import watcher


class _InertWatcher(threading.Thread):
    """A watcher that parks on its stop event instead of dialling a device."""

    def __init__(self, device_id, name, creds):
        super().__init__(daemon=True)
        self.device_id, self.device_name, self.creds = device_id, name, creds
        self.fingerprint = watcher._fingerprint(name, creds)
        self.stop_event = threading.Event()
        self.connected = True
        self.last_ring = None
        self.last_error = None

    def run(self):
        self.stop_event.wait()


@pytest.fixture
def watchers(monkeypatch):
    """Isolate the watcher registry and keep every thread off the network."""
    init_db()
    monkeypatch.setattr(watcher, "_DeviceWatcher", _InertWatcher)
    monkeypatch.setattr(watcher.settings_store, "monitor_enabled", lambda: True)
    ring.stop()
    with session_scope() as db:
        db.query(Device).delete()
    yield watcher
    ring.stop()
    with session_scope() as db:
        db.query(Device).delete()


def _add_device(**overrides) -> int:
    fields = {
        "name": "front",
        "host": "10.0.0.5",
        "username": "olduser",
        "password_enc": encrypt("oldpass"),
        "enabled": True,
    }
    fields.update(overrides)
    with session_scope() as db:
        d = Device(**fields)
        db.add(d)
        db.flush()
        return d.id


def _only_watcher(mod):
    live = list(mod._watchers.values())
    assert len(live) == 1, f"expected exactly one watcher, got {len(live)}"
    return live[0]


def test_editing_a_password_restarts_the_watcher(watchers):
    device_id = _add_device()
    watchers.start()
    before = _only_watcher(watchers)
    assert before.creds.password == "oldpass"

    with session_scope() as db:
        db.get(Device, device_id).password_enc = encrypt("newpass")
    watchers.refresh()

    after = _only_watcher(watchers)
    assert after is not before, "the stale watcher was kept"
    assert after.creds.password == "newpass"
    assert before.stop_event.is_set(), "the replaced watcher was never told to stop"


def test_editing_the_host_restarts_the_watcher(watchers):
    device_id = _add_device()
    watchers.start()
    before = _only_watcher(watchers)

    with session_scope() as db:
        db.get(Device, device_id).host = "10.0.0.99"
    watchers.refresh()

    after = _only_watcher(watchers)
    assert after is not before
    assert after.creds.host == "10.0.0.99"


def test_renaming_a_device_restarts_the_watcher(watchers):
    """Otherwise `status()` keeps reporting the old name on the settings page."""
    device_id = _add_device()
    watchers.start()
    before = _only_watcher(watchers)

    with session_scope() as db:
        db.get(Device, device_id).name = "side gate"
    watchers.refresh()

    after = _only_watcher(watchers)
    assert after is not before
    assert after.device_name == "side gate"


def test_an_unchanged_device_keeps_its_watcher(watchers):
    """The flip side: refresh must not churn threads on every unrelated save."""
    _add_device()
    watchers.start()
    before = _only_watcher(watchers)

    watchers.refresh()
    watchers.refresh()

    assert _only_watcher(watchers) is before
    assert not before.stop_event.is_set()


def test_disabling_a_device_stops_its_watcher(watchers):
    device_id = _add_device()
    watchers.start()
    before = _only_watcher(watchers)

    with session_scope() as db:
        db.get(Device, device_id).enabled = False
    watchers.refresh()

    assert watcher._watchers == {}
    assert before.stop_event.is_set()


def test_stop_waits_for_watchers_to_finish(watchers):
    _add_device()
    watchers.start()
    before = _only_watcher(watchers)

    watchers.stop()

    assert watcher._watchers == {}
    assert before.stop_event.is_set()
    # `stop()` joins, so by the time it returns the thread is genuinely gone
    # rather than merely dropped from the registry.
    assert not before.is_alive()


def test_fingerprint_covers_every_field_a_watcher_holds():
    """A new credential field must be added here, or edits to it go unnoticed."""
    base = DeviceCreds(host="10.0.0.5", username="u", password="p", use_https=False)
    reference = watcher._fingerprint("front", base)

    variations = [
        ("side", base),
        ("front", DeviceCreds(host="10.0.0.6", username="u", password="p", use_https=False)),
        ("front", DeviceCreds(host="10.0.0.5", username="v", password="p", use_https=False)),
        ("front", DeviceCreds(host="10.0.0.5", username="u", password="q", use_https=False)),
        ("front", DeviceCreds(host="10.0.0.5", username="u", password="p", use_https=True)),
    ]
    for name, creds in variations:
        assert watcher._fingerprint(name, creds) != reference


# ------------------------------------------------ a key that no longer fits


def test_an_undecryptable_password_does_not_stop_the_app(watchers, caplog):
    """A rotated or lost FERNET_KEY must not take startup down.

    Discovered by restoring a database into a container with a different key:
    `Fernet.decrypt` raised a bare `binascii.Error` out of `ring.start`,
    the lifespan blew up, and the app exited with no usable explanation.
    """
    import logging

    with session_scope() as db:
        db.add(Device(name="broken", host="10.0.0.7", username="u",
                      password_enc="not-a-fernet-token", enabled=True))
        db.add(Device(name="fine", host="10.0.0.8", username="u",
                      password_enc=encrypt("goodpass"), enabled=True))

    with caplog.at_level(logging.ERROR):
        watchers.start()          # must not raise

    live = {w.device_name for w in watcher._watchers.values()}
    assert live == {"fine"}, "the healthy device should still be watched"
    assert any("FERNET_KEY" in r.getMessage() for r in caplog.records), \
        "the failure was not reported"

    # And the reason is recorded where the user will see it.
    with session_scope() as db:
        broken = db.query(Device).filter(Device.name == "broken").one()
        assert broken.last_error and "FERNET_KEY" in broken.last_error


def test_decrypt_raises_a_named_error_not_binascii():
    from app.crypto import UndecryptableSecret, decrypt

    with pytest.raises(UndecryptableSecret):
        decrypt("not-a-fernet-token")
    with pytest.raises(UndecryptableSecret):
        decrypt("")
