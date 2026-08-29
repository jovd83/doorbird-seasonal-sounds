"""Per-device listener threads and their lifecycle.

One daemon thread per enabled device holds a `monitor.cgi` connection open and
waits for `doorbell:H`. When it sees one it hands off to `app.ring.playback`.

Design notes:

* Playback happens on a separate worker thread so a 6-second chime never
  blocks the event stream — a visitor leaning on the button would otherwise
  queue up events behind the audio.
* `monitor.cgi` reports the state of every requested input the moment it
  connects, so the first snapshot is swallowed: only a transition into `H`
  counts as a ring.
* The device drops idle connections and reboots for firmware updates, so each
  watcher reconnects with capped exponential backoff and treats a clean
  stream close as a normal reconnect rather than an error.
* The stream carries a read timeout so a watcher can notice its own stop flag
  even when the doorbell is silent.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from app import settings_store
from app.config import settings
from app.crypto import UndecryptableSecret, decrypt
from app.db import session_scope
from app.doorbird import DeviceCreds, DoorBirdClient, DoorBirdError
from app.models import Device
from app.ring.debounce import claim_ring
from app.ring.playback import play_active_chime

log = logging.getLogger("doorbird.ring")

BACKOFF_START = 2.0
BACKOFF_MAX = 60.0

# How long the monitor stream may sit silent before we drop it and reconnect.
# This is what makes a watcher stoppable: the read would otherwise block
# indefinitely and the thread could not notice its own stop flag.
MONITOR_IDLE_TIMEOUT = 90.0
# How long `stop()` waits for a watcher to actually finish.
STOP_JOIN_TIMEOUT = 3.0

_watchers: dict[int, _DeviceWatcher] = {}
_lock = threading.Lock()


def _fingerprint(name: str, creds: DeviceCreds) -> tuple[str, str, str, str, bool, bool]:
    """Everything a watcher was built from.

    A running watcher holds the credentials it was constructed with, so editing
    a device used to change nothing until the container restarted: `refresh()`
    only stopped watchers for *disabled* devices, and `start()` skipped any
    device that already had a live thread. Comparing this tuple is what makes
    an edit reach the listener.
    """
    return (name, creds.clean_host, creds.username, creds.password,
            creds.use_https, creds.verify_tls)


class _DeviceWatcher(threading.Thread):
    def __init__(self, device_id: int, name: str, creds: DeviceCreds):
        super().__init__(name=f"ring-{name}", daemon=True)
        self.device_id = device_id
        self.device_name = name
        self.creds = creds
        self.fingerprint = _fingerprint(name, creds)
        self.stop_event = threading.Event()
        self.connected = False
        self.last_ring: datetime | None = None
        self.last_error: str | None = None

    # ---------------------------------------------------------------- run

    def run(self) -> None:
        backoff = BACKOFF_START
        log.info("[%s] ring watcher starting", self.device_name)
        while not self.stop_event.is_set():
            try:
                self._consume_stream()
                backoff = BACKOFF_START          # clean close -> just reconnect
            except DoorBirdError as exc:
                self.last_error = str(exc)
                log.warning("[%s] ring stream: %s", self.device_name, exc)
            except Exception as exc:
                self.last_error = repr(exc)
                log.warning("[%s] ring stream error: %r", self.device_name, exc)
            finally:
                self.connected = False

            if self.stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, BACKOFF_MAX)

        log.info("[%s] ring watcher stopped", self.device_name)

    def _consume_stream(self) -> None:
        with DoorBirdClient(self.creds) as client:
            seen_snapshot = False
            for name, state in client.stream_ring_events(
                timeout=MONITOR_IDLE_TIMEOUT
            ):
                if self.stop_event.is_set():
                    return
                if not self.connected:
                    self.connected = True
                    self.last_error = None
                    log.info("[%s] ring stream connected", self.device_name)

                if name != "doorbell":
                    continue
                if not seen_snapshot:
                    # First doorbell line is the current level, not a press.
                    seen_snapshot = True
                    if state == "L":
                        continue
                if state == "H":
                    self._on_ring()

    # --------------------------------------------------------------- ring

    def _on_ring(self) -> None:
        if not claim_ring(self.device_id):
            log.info("[%s] ring ignored (debounce)", self.device_name)
            return
        self.last_ring = datetime.now()
        log.info("[%s] RING via monitor.cgi", self.device_name)
        threading.Thread(
            target=self._play_safely, name=f"chime-{self.device_name}", daemon=True
        ).start()

    def _play_safely(self) -> None:
        try:
            ok, msg = play_active_chime(self.device_id)
            log.info("[%s] chime: %s", self.device_name, msg)
            if not ok:
                self.last_error = msg
        except Exception as exc:
            log.exception("[%s] chime crashed", self.device_name)
            self.last_error = repr(exc)




def _creds_for(device: Device) -> DeviceCreds:
    return DeviceCreds(
        host=device.host,
        username=device.username,
        password=decrypt(device.password_enc),
        use_https=device.use_https,
        verify_tls=device.verify_tls,
    )


def _startable_devices(devices: list[Device]) -> dict[int, tuple[str, DeviceCreds]]:
    """Devices we can actually build credentials for.

    One device whose password will not decrypt -- a rotated or lost
    FERNET_KEY, or a database restored without its .env -- used to take the
    whole application down at startup with a `binascii.Error` and no
    explanation. It is now a per-device problem: that listener does not start,
    the reason is logged and recorded against the device, and every other door
    station carries on.
    """
    startable: dict[int, tuple[str, DeviceCreds]] = {}
    for d in devices:
        try:
            startable[d.id] = (d.name, _creds_for(d))
        except UndecryptableSecret as exc:
            log.error("[%s] cannot start ring watcher: %s", d.name, exc)
            d.last_error = str(exc)
    return startable


def start() -> None:
    """Start a monitor.cgi watcher per enabled device, if that trigger is chosen."""
    if not settings.ring_chime_enabled:
        log.info("ring chime disabled (RING_CHIME_ENABLED=0)")
        return
    if not settings_store.monitor_enabled():
        log.info("trigger mode is %r - not starting monitor.cgi watchers",
                 settings_store.trigger_mode())
        stop()
        return

    with session_scope() as db:
        wanted = _startable_devices(db.query(Device).filter(Device.enabled.is_(True)).all())

    with _lock:
        for device_id, (name, creds) in wanted.items():
            existing = _watchers.get(device_id)
            if existing and existing.is_alive():
                if existing.fingerprint == _fingerprint(name, creds):
                    continue
                # The device row changed under a running thread. Retire it and
                # build a new one, or it keeps dialling the old address with
                # the old password until the container restarts.
                log.info("[%s] device settings changed; restarting ring watcher", name)
                existing.stop_event.set()
            w = _DeviceWatcher(device_id, name, creds)
            _watchers[device_id] = w
            w.start()


def stop() -> None:
    """Signal every watcher and wait briefly for it to finish.

    Joining matters: without it `status()` reports no listeners while threads
    are still holding connections open, and a device re-enabled soon after
    being disabled ends up with two watchers on the same stream.
    """
    with _lock:
        watchers = list(_watchers.values())
        _watchers.clear()

    for w in watchers:
        w.stop_event.set()

    # One shared deadline, not one per watcher: the threads wind down in
    # parallel, so waiting `STOP_JOIN_TIMEOUT` each would make shutdown scale
    # with the number of door stations for no benefit.
    deadline = time.monotonic() + STOP_JOIN_TIMEOUT
    for w in watchers:
        w.join(timeout=max(0.0, deadline - time.monotonic()))

    stubborn = [w.device_name for w in watchers if w.is_alive()]
    if stubborn:
        # Daemon threads, so this never blocks process exit -- but a watcher
        # parked in `connect()` cannot see its stop flag, and saying so beats
        # reporting a clean shutdown that did not happen.
        log.warning(
            "ring watcher(s) %s still winding down; they exit at their next idle "
            "timeout (%.0fs)", ", ".join(stubborn), MONITOR_IDLE_TIMEOUT,
        )


def refresh() -> None:
    """Re-sync watchers with the device table and the chosen trigger mode."""
    if not settings.ring_chime_enabled:
        return
    if not settings_store.monitor_enabled():
        stop()
        return
    with session_scope() as db:
        enabled = {d.id for d in db.query(Device).filter(Device.enabled.is_(True)).all()}

    with _lock:
        stale = [_watchers.pop(d) for d in list(_watchers) if d not in enabled]
    for w in stale:
        w.stop_event.set()
    # `start()` handles the rest: it now compares each live watcher's
    # fingerprint against the current row and replaces any that has drifted.
    start()


@dataclass(frozen=True)
class WatcherStatus:
    """A snapshot of one listener, for the dashboard and the settings page.

    A dataclass rather than a dict: this is read from two templates and from
    `templating._shell()`, all by name. As a dict a renamed key surfaced as a
    KeyError inside a Jinja render -- on a page the user was already looking
    at -- instead of failing at import.
    """

    device_id: int
    device: str
    alive: bool
    connected: bool
    last_ring: datetime | None
    last_error: str | None

    @property
    def reconnecting(self) -> bool:
        """Running but not currently attached.

        Routine rather than a fault: the device drops idle connections and
        reboots for firmware updates.
        """
        return self.alive and not self.connected


def status() -> list[WatcherStatus]:
    with _lock:
        watchers = list(_watchers.values())
    return [
        WatcherStatus(
            device_id=w.device_id,
            device=w.device_name,
            alive=w.is_alive(),
            connected=w.connected,
            last_ring=w.last_ring,
            last_error=w.last_error,
        )
        for w in watchers
    ]
