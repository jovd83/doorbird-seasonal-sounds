"""State the app shell shows on every page, resolved once per request.

This used to be two Jinja globals — `shell()` and `theme()` — that each opened
their own database session *during template rendering*. Measured, that was
three extra sessions on a typical page and ten on `/settings`: outside the
request's transaction, invisible to the route, and impossible to stub in a
template test. It also meant the error page hit the database, so a database
failure produced a second exception instead of an error page.

Now a dependency resolves it before the handler runs and hangs it on
`request.state`. The template reads an attribute and touches nothing.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.config import settings


@dataclass(frozen=True)
class ShellState:
    """Everything `base.html` needs that is not page-specific."""

    theme: str
    trigger_label: str
    listener_tone: str
    listener_label: str


# Used for non-GET requests (which redirect rather than render) and as the
# fallback when a template is rendered outside a request that resolved one.
# Deliberately inert: it must never need a query to construct.
DEFAULT_SHELL = ShellState(
    theme="dark",
    trigger_label="",
    listener_tone="is-idle",
    listener_label="",
)


def build_shell_state() -> ShellState:
    """Read the live trigger mode and listener health. One session, one call."""
    from app import ring, settings_store

    # One read of the settings table, not one per key.
    snap = settings_store.snapshot()
    mode = settings_store.trigger_mode(snap)
    watchers = ring.status()
    total = len(watchers)
    live = sum(1 for w in watchers if w.connected)
    # A watcher that is alive but not yet connected is reconnecting, which is
    # routine -- the device drops idle connections and reboots for firmware
    # updates. Only a thread that has actually stopped is a failure.
    stopped = sum(1 for w in watchers if not w.alive)
    reconnecting = total - live - stopped

    if not settings.ring_chime_enabled:
        tone, label = "is-idle", "ring chime off"
    elif mode == settings_store.MODE_WEBHOOK:
        tone, label = "is-live", "webhook only"
    elif total == 0:
        tone, label = "is-idle", "no listeners"
    elif stopped:
        tone, label = "is-down", f"{stopped} of {total} listeners stopped"
    elif reconnecting:
        tone, label = "is-warn", f"{reconnecting} reconnecting"
    else:
        tone, label = "is-live", f"{live} listener{'s' if live != 1 else ''} live"

    return ShellState(
        theme=settings_store.theme(snap),
        trigger_label=settings_store.TRIGGER_MODES[mode],
        listener_tone=tone,
        listener_label=label,
    )


def resolve_shell(request: Request) -> ShellState:
    """FastAPI dependency: resolve the shell once and stash it on the request.

    Skipped for anything that is not a GET. Mutating routes answer with a
    redirect and never render the shell, so resolving it there would be a
    query bought for nothing.
    """
    if request.method != "GET":
        request.state.shell = DEFAULT_SHELL
        return DEFAULT_SHELL

    state = build_shell_state()
    request.state.shell = state
    return state


def shell_for(request: Request) -> ShellState:
    """What the template calls. Never queries; reads what the dependency left."""
    return getattr(request.state, "shell", DEFAULT_SHELL)
