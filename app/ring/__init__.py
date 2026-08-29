"""Everything that happens when the doorbell is pressed.

Split out of a single `ring_watcher` module that had grown to mix three jobs:
the shared debounce, the audio playback every trigger source ends up calling,
and the listener threads with their lifecycle.

    debounce   one ring, one chime, whichever trigger reported it
    playback   the chime and the spoken auto response that may follow
    watcher    per-device monitor.cgi listeners and their lifecycle

The names re-exported here are the module's public surface — the routes and
the scheduler import from `app.ring`, not from its parts.
"""
from app.ring.debounce import claim_ring, trigger_ring
from app.ring.debounce import reset as reset_debounce
from app.ring.playback import (
    cancel_pending_auto_responses,
    play_active_chime,
    play_auto_response,
    schedule_auto_response,
)
from app.ring.watcher import (
    MONITOR_IDLE_TIMEOUT,
    STOP_JOIN_TIMEOUT,
    WatcherStatus,
    refresh,
    start,
    status,
    stop,
)

__all__ = [
    "MONITOR_IDLE_TIMEOUT",
    "STOP_JOIN_TIMEOUT",
    "WatcherStatus",
    "cancel_pending_auto_responses",
    "claim_ring",
    "play_active_chime",
    "play_auto_response",
    "refresh",
    "reset_debounce",
    "schedule_auto_response",
    "start",
    "status",
    "stop",
    "trigger_ring",
]
