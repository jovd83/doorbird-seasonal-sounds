"""One ring, one chime — whichever trigger reports it.

Shared across *all* trigger sources rather than held per watcher: in "both"
mode the monitor stream and the DoorBird's own HTTP favourite report the same
press within milliseconds of each other, and only one chime should come out of
the speaker.
"""
from __future__ import annotations

import logging
import threading
import time

from app.config import settings

log = logging.getLogger("doorbird.ring")

_last_fire: dict[int, float] = {}
_fire_lock = threading.Lock()


def claim_ring(device_id: int) -> bool:
    """Return True if this ring should chime, False if it's a duplicate."""
    now = time.monotonic()
    with _fire_lock:
        previous = _last_fire.get(device_id, 0.0)
        if now - previous < settings.ring_debounce_seconds:
            return False
        _last_fire[device_id] = now
        return True


def trigger_ring(device_id: int, source: str) -> tuple[bool, str]:
    """Handle a ring from any trigger source, applying the shared debounce."""
    if not claim_ring(device_id):
        log.info("[device %s] ring via %s ignored (debounce)", device_id, source)
        return False, "ignored (debounce)"
    log.info("[device %s] RING via %s", device_id, source)
    # Imported here rather than at module scope: playback imports the
    # debounce, so a top-level import would be a cycle.
    from app.ring.playback import play_active_chime

    return play_active_chime(device_id)




def reset() -> None:
    """Forget every recorded ring. For tests and for a clean shutdown."""
    with _fire_lock:
        _last_fire.clear()
