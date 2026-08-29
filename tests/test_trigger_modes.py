"""Trigger-mode selection, the shared debounce, and the ring webhook.

The behaviour that matters most here is that a single button press produces a
single chime even when both trigger sources are active, and that the webhook
is genuinely closed when it is not the selected mode.
"""
from __future__ import annotations

import threading
import time

import pytest

from app import ring, settings_store
from app.config import settings
from app.crypto import encrypt
from app.db import init_db, session_scope
from app.main import app
from app.models import Device
from app.ring import debounce, playback

# Environment isolation lives in conftest.py so it is applied before any import.
from tests.conftest import FormClient as TestClient


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()
    with session_scope() as db:
        if not db.query(Device).filter(Device.name == "TRIG").first():
            db.add(Device(name="TRIG", host="10.0.0.9", username="u",
                          password_enc=encrypt("p"), enabled=True))
    yield


@pytest.fixture(autouse=True)
def _reset_debounce():
    _settle_auto_responses()
    debounce._last_fire.clear()
    yield
    # `play_active_chime` queues the auto response on its own thread and
    # returns. Left running, that thread wakes up inside the *next* test and
    # calls the `ensure_ulaw` stub it has monkeypatched, which is how
    # "no audio should have been prepared" failed intermittently.
    _settle_auto_responses()
    debounce._last_fire.clear()


def _settle_auto_responses() -> None:
    """Cancel every pending auto response and wait for its thread to finish."""
    ring.cancel_pending_auto_responses()
    for t in threading.enumerate():
        if t.name.startswith("auto-response-"):
            t.join(timeout=5)


def _device_id() -> int:
    with session_scope() as db:
        return db.query(Device).filter(Device.name == "TRIG").first().id


# --- mode selection -----------------------------------------------------

def test_default_mode_is_the_passive_monitor():
    settings_store.set_value(settings_store.TRIGGER_MODE, "")
    assert settings_store.trigger_mode() == settings_store.MODE_MONITOR
    assert settings_store.monitor_enabled() is True
    assert settings_store.webhook_enabled() is False


def test_webhook_mode_stops_the_monitor_listeners():
    settings_store.set_trigger_mode(settings_store.MODE_WEBHOOK)
    assert settings_store.monitor_enabled() is False
    assert settings_store.webhook_enabled() is True


def test_both_mode_enables_each_source():
    settings_store.set_trigger_mode(settings_store.MODE_BOTH)
    assert settings_store.monitor_enabled() is True
    assert settings_store.webhook_enabled() is True


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        settings_store.set_trigger_mode("carrier-pigeon")


def test_a_garbage_stored_mode_falls_back_to_monitor():
    settings_store.set_value(settings_store.TRIGGER_MODE, "nonsense")
    assert settings_store.trigger_mode() == settings_store.MODE_MONITOR


# --- shared debounce ----------------------------------------------------

def test_two_sources_reporting_one_press_chime_once(monkeypatch):
    """The whole point of 'both' mode not doubling the sound."""
    monkeypatch.setattr(settings, "ring_debounce_seconds", 30.0)
    dev = _device_id()
    assert ring.claim_ring(dev) is True     # monitor.cgi got there first
    assert ring.claim_ring(dev) is False    # webhook arrives milliseconds later


def test_debounce_is_per_device(monkeypatch):
    monkeypatch.setattr(settings, "ring_debounce_seconds", 30.0)
    assert ring.claim_ring(1) is True
    assert ring.claim_ring(2) is True       # a different door is not a duplicate


def test_debounce_releases_once_the_window_passes(monkeypatch):
    monkeypatch.setattr(settings, "ring_debounce_seconds", 0.0)
    dev = _device_id()
    assert ring.claim_ring(dev) is True
    assert ring.claim_ring(dev) is True


# --- webhook endpoint ---------------------------------------------------

def test_webhook_is_invisible_when_not_the_selected_mode():
    settings_store.set_trigger_mode(settings_store.MODE_MONITOR)
    token = settings_store.webhook_token()
    with TestClient(app) as c:
        assert c.get(f"/ring/{token}").status_code == 404


def test_webhook_rejects_a_wrong_token():
    settings_store.set_trigger_mode(settings_store.MODE_WEBHOOK)
    with TestClient(app) as c:
        assert c.get("/ring/definitely-not-the-token").status_code == 404


def test_webhook_fires_the_chime(monkeypatch):
    settings_store.set_trigger_mode(settings_store.MODE_WEBHOOK)
    monkeypatch.setattr(settings, "ring_debounce_seconds", 0.0)
    done = threading.Event()
    calls: list[int] = []

    def _record(did):
        calls.append(did)
        done.set()
        return True, "played"

    monkeypatch.setattr(ring, "play_active_chime", _record)

    token = settings_store.webhook_token()
    with TestClient(app) as c:
        r = c.get(f"/ring/{token}?device=TRIG")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert done.wait(5), "chime was never dispatched"
    assert calls == [_device_id()]


def test_webhook_answers_without_waiting_for_the_audio(monkeypatch):
    """A DoorBird favourite must not hang for the length of the clip."""
    settings_store.set_trigger_mode(settings_store.MODE_WEBHOOK)
    monkeypatch.setattr(settings, "ring_debounce_seconds", 0.0)
    monkeypatch.setattr(ring, "play_active_chime",
                        lambda did: (time.sleep(2.0), (True, "played"))[1])

    token = settings_store.webhook_token()
    with TestClient(app) as c:
        started = time.monotonic()          # time the request only, not startup
        r = c.get(f"/ring/{token}?device=TRIG")
        elapsed = time.monotonic() - started
    assert r.status_code == 200
    assert elapsed < 1.0, f"webhook blocked for {elapsed:.2f}s while the chime played"


def test_webhook_rejects_an_immediate_duplicate(monkeypatch):
    """Monitor and webhook reporting the same press must chime once."""
    settings_store.set_trigger_mode(settings_store.MODE_BOTH)
    monkeypatch.setattr(settings, "ring_debounce_seconds", 30.0)
    monkeypatch.setattr(ring, "play_active_chime", lambda did: (True, "played"))

    token = settings_store.webhook_token()
    with TestClient(app) as c:
        first = c.get(f"/ring/{token}?device=TRIG").json()
        second = c.get(f"/ring/{token}?device=TRIG").json()
    assert first["ok"] is True
    assert second["ok"] is False
    assert "debounce" in second["detail"]


def test_webhook_accepts_a_numeric_device_id(monkeypatch):
    settings_store.set_trigger_mode(settings_store.MODE_WEBHOOK)
    monkeypatch.setattr(settings, "ring_debounce_seconds", 0.0)
    monkeypatch.setattr(ring, "play_active_chime", lambda did: (True, "played"))

    token = settings_store.webhook_token()
    with TestClient(app) as c:
        assert c.get(f"/ring/{token}?device={_device_id()}").status_code == 200


def test_webhook_404s_for_an_unknown_device(monkeypatch):
    settings_store.set_trigger_mode(settings_store.MODE_WEBHOOK)
    token = settings_store.webhook_token()
    with TestClient(app) as c:
        assert c.get(f"/ring/{token}?device=NoSuchDoor").status_code == 404


def test_rotating_the_token_invalidates_the_old_url():
    settings_store.set_trigger_mode(settings_store.MODE_WEBHOOK)
    old = settings_store.webhook_token()
    new = settings_store.rotate_webhook_token()
    assert old != new
    with TestClient(app) as c:
        assert c.get(f"/ring/{old}").status_code == 404


def test_forward_url_round_trips():
    settings_store.set_webhook_forward_url("  http://1.2.3.4/pulse?doorbell  ")
    assert settings_store.webhook_forward_url() == "http://1.2.3.4/pulse?doorbell"
    settings_store.set_webhook_forward_url("")
    assert settings_store.webhook_forward_url() == ""


def test_forward_url_credentials_are_redacted_in_logs():
    from app.routes.ring_hook import _redact
    out = _redact("http://someuser:sekrit@10.0.0.5/some/ring/endpoint")
    assert "sekrit" not in out
    assert "10.0.0.5" in out


# --- "play default when no schedule is active" toggle -------------------

def test_default_playback_is_on_out_of_the_box():
    """Existing installs must keep playing the default; this is opt-out."""
    settings_store.set_value(settings_store.PLAY_DEFAULT_IDLE, "")
    assert settings_store.play_default_when_idle() is True


def test_toggle_round_trips():
    settings_store.set_play_default_when_idle(False)
    assert settings_store.play_default_when_idle() is False
    settings_store.set_play_default_when_idle(True)
    assert settings_store.play_default_when_idle() is True


def test_silent_when_no_schedule_matches_and_toggle_is_off(monkeypatch):
    """A ring off-season sends no audio, and is not recorded as a failure."""
    from app.date_logic import Resolution
    from app.models import Mp3File

    settings_store.set_play_default_when_idle(False)
    fallback = Mp3File(id=99, label="default", filename="d.mp3", size_bytes=1)
    # schedule=None is what "nothing matched, using the default" looks like.
    monkeypatch.setattr(
        playback, "today_resolution",
        lambda db, **kw: Resolution(schedule=None, mp3=fallback, reason="no schedule active"),
    )
    played: list[str] = []
    monkeypatch.setattr(playback, "ensure_ulaw",
                        lambda p: played.append(str(p)) or p)

    ok, msg = ring.play_active_chime(_device_id())
    assert ok is True                      # not an error condition
    assert "staying silent" in msg
    assert played == [], "no audio should have been prepared"


def test_default_still_plays_when_toggle_is_on(monkeypatch):
    from app.date_logic import Resolution
    from app.models import Mp3File

    settings_store.set_play_default_when_idle(True)
    fallback = Mp3File(id=99, label="default", filename="d.mp3", size_bytes=1)
    monkeypatch.setattr(
        playback, "today_resolution",
        lambda db, **kw: Resolution(schedule=None, mp3=fallback, reason="no schedule active"),
    )
    reached: list[str] = []
    monkeypatch.setattr(playback, "ensure_ulaw",
                        lambda p: (reached.append(str(p)), p)[1])
    monkeypatch.setattr(playback, "DoorBirdClient", lambda creds: _FakeChime())

    ok, msg = ring.play_active_chime(_device_id())
    assert reached, "the default should have been transcoded and played"
    assert ok is True and "played" in msg


class _FakeChime:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def play_ulaw(self, path, **kw):
        return f"played {path}"


def test_interactive_api_docs_are_closed_by_default():
    """/docs and /openapi.json need no login and list every route."""
    with TestClient(app) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
        assert c.get("/redoc").status_code == 404
