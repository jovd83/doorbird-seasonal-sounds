"""Tests for the ring-detection logic and the mu-law cache.

The two things worth pinning down here are the ones that would silently
misbehave in production: `monitor.cgi`'s opening state snapshot must not be
mistaken for a ring, and a held button must not fire the chime repeatedly.
"""
from __future__ import annotations

import os

import pytest

# Environment isolation lives in conftest.py so it is applied before any import.
from app.audio import ULAW_RATE, _cache_name
from app.config import settings
from app.doorbird import DeviceCreds
from app.ring import debounce, watcher


class _FakeClient:
    """Stands in for DoorBirdClient, replaying a scripted event stream."""

    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream_ring_events(self, **_kw):
        yield from self._events


@pytest.fixture(autouse=True)
def _reset_shared_debounce():
    """The debounce is module-level now, so it leaks across tests otherwise."""
    debounce._last_fire.clear()
    yield
    debounce._last_fire.clear()


def _watcher(monkeypatch, events):
    w = watcher._DeviceWatcher(1, "RD29", DeviceCreds("1.2.3.4", "u", "p"))
    monkeypatch.setattr(watcher, "DoorBirdClient", lambda creds: _FakeClient(events))
    fired: list[int] = []
    monkeypatch.setattr(w, "_play_safely", lambda: fired.append(1))
    # Fire synchronously so the assertions don't race a worker thread.
    # Scoped to the watcher module, not the global `threading`: patching that
    # would also catch any thread another test happens to start concurrently.
    monkeypatch.setattr(
        watcher.threading, "Thread",
        lambda target, **kw: type("T", (), {"start": staticmethod(target)})(),
    )
    return w, fired


def test_opening_snapshot_is_not_a_ring(monkeypatch):
    """monitor.cgi reports current levels on connect; those aren't presses."""
    w, fired = _watcher(monkeypatch, [("doorbell", "L"), ("motionsensor", "L")])
    w._consume_stream()
    assert fired == []


def test_press_after_snapshot_fires_once(monkeypatch):
    w, fired = _watcher(monkeypatch, [
        ("doorbell", "L"),      # snapshot
        ("doorbell", "H"),      # press
        ("doorbell", "L"),      # release
    ])
    w._consume_stream()
    assert len(fired) == 1


def test_motion_events_never_chime(monkeypatch):
    w, fired = _watcher(monkeypatch, [
        ("doorbell", "L"),
        ("motionsensor", "H"),
        ("motionsensor", "H"),
    ])
    w._consume_stream()
    assert fired == []


def test_debounce_suppresses_a_held_button(monkeypatch):
    monkeypatch.setattr(settings, "ring_debounce_seconds", 30.0)
    w, fired = _watcher(monkeypatch, [
        ("doorbell", "L"),
        ("doorbell", "H"),
        ("doorbell", "L"),
        ("doorbell", "H"),      # second press within the debounce window
        ("doorbell", "L"),
        ("doorbell", "H"),
    ])
    w._consume_stream()
    assert len(fired) == 1


def test_debounce_window_allows_a_later_ring(monkeypatch):
    monkeypatch.setattr(settings, "ring_debounce_seconds", 0.0)
    w, fired = _watcher(monkeypatch, [
        ("doorbell", "L"),
        ("doorbell", "H"),
        ("doorbell", "H"),
    ])
    w._consume_stream()
    assert len(fired) == 2


def test_snapshot_that_is_already_high_counts_as_a_ring(monkeypatch):
    """Connecting mid-press should still chime rather than swallow the event."""
    w, fired = _watcher(monkeypatch, [("doorbell", "H")])
    w._consume_stream()
    assert len(fired) == 1


def test_ulaw_cache_key_changes_with_content(tmp_path):
    src = tmp_path / "chime.mp3"
    src.write_bytes(b"a" * 100)
    first = _cache_name(src)

    src.write_bytes(b"b" * 200)          # different size -> different key
    os.utime(src, (0, 0))
    assert _cache_name(src) != first


def test_ulaw_rate_matches_one_byte_per_sample():
    """8 kHz mono mu-law is exactly 8000 bytes of payload per second."""
    assert ULAW_RATE == 8000


def test_ulaw_dir_is_under_data_dir():
    assert settings.ulaw_dir.parent == settings.data_dir
    assert settings.ulaw_dir.name == "ulaw"


class _Sock:
    """Minimal socket stand-in that replays a canned HTTP status line."""

    def __init__(self, status: str):
        self.status = status
        self.sent = 0

    def settimeout(self, _t): pass

    def sendall(self, b): self.sent += len(b)

    def recv(self, _n):
        line, self.status = self.status, b""
        return line

    def close(self): pass


def test_play_ulaw_retries_while_channel_is_busy(monkeypatch, tmp_path):
    """A phone holding live view returns 503; we should retry, then succeed."""
    # The socket protocol lives in `audio_transmit`, not in the HTTP client.
    from app.doorbird import audio_transmit as at
    from app.doorbird import client as dc

    audio = tmp_path / "c.ulaw"
    audio.write_bytes(b"\xff" * 800)

    replies = [b"HTTP/1.0 503 Service Not Available\r\n",
               b"HTTP/1.0 503 Service Not Available\r\n",
               b"HTTP/1.0 200 OK\r\n"]
    made: list[_Sock] = []

    def fake_conn(_addr, timeout=None):
        s = _Sock(replies[len(made)])
        made.append(s)
        return s

    monkeypatch.setattr(at.socket, "create_connection", fake_conn)
    monkeypatch.setattr(at.time, "sleep", lambda _s: None)

    client = dc.DoorBirdClient(DeviceCreds("1.2.3.4", "u", "p"))
    assert "played" in client.play_ulaw(audio, retries=3, retry_wait=0)
    assert len(made) == 3


def test_play_ulaw_gives_up_if_channel_stays_busy(monkeypatch, tmp_path):
    from app.doorbird import audio_transmit as at
    from app.doorbird import client as dc

    audio = tmp_path / "c.ulaw"
    audio.write_bytes(b"\xff" * 800)

    monkeypatch.setattr(
        at.socket, "create_connection",
        lambda _a, timeout=None: _Sock(b"HTTP/1.0 503 Service Not Available\r\n"),
    )
    monkeypatch.setattr(at.time, "sleep", lambda _s: None)

    client = dc.DoorBirdClient(DeviceCreds("1.2.3.4", "u", "p"))
    with pytest.raises(dc.DoorBirdError) as exc:
        client.play_ulaw(audio, retries=2, retry_wait=0)
    assert "already" in str(exc.value)
