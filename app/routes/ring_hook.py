"""Public ring webhook — the endpoint a DoorBird HTTP favourite calls.

Unlike every other route in this app this one is **not** session-protected:
the door station cannot log in or present a cookie. The secret is the URL
itself, which is only ever configured onto a device on the local network.

The device fires this as a plain GET with no body, so both GET and POST are
accepted and the device is identified by a query parameter.

    GET /ring/<token>?device=1

Responses are deliberately terse and always 200 for a valid token: DoorBird
retries and logs failures, and a ring that arrives while the speaker is busy
is not an error worth alarming anyone about.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Query

from app import ring as ring_service

# Aliased: the route handler below is itself called `ring`.
from app import settings_store
from app.db import session_scope
from app.models import Device

log = logging.getLogger("doorbird.hook")

router = APIRouter()


def _resolve_device(device: str | None) -> int:
    """Accept a device id or name; fall back to the only device if unambiguous."""
    with session_scope() as db:
        if device:
            key = device.strip()
            if key.isdigit():
                found = db.get(Device, int(key))
            else:
                found = db.query(Device).filter(Device.name == key).first()
            if found is None:
                raise HTTPException(404, f"no device matching {key!r}")
            return found.id

        devices = db.query(Device).filter(Device.enabled.is_(True)).all()
        if len(devices) == 1:
            return devices[0].id
        raise HTTPException(
            400,
            "several devices are enabled — add ?device=<id or name> to the webhook URL",
        )


@router.api_route("/ring/{token}", methods=["GET", "POST"])
async def ring(token: str, device: str | None = Query(default=None)):
    if not settings_store.webhook_enabled():
        # Wrong token and disabled webhook look identical from outside.
        raise HTTPException(404, "not found")
    if not _tokens_match(token, settings_store.webhook_token()):
        log.warning("ring webhook called with an invalid token")
        raise HTTPException(404, "not found")

    device_id = _resolve_device(device)

    # Relay first: whatever we displaced from the doorbell's HTTP slot is
    # somebody's real automation (door opener, indoor chime) and must not wait
    # behind our audio.
    forward = settings_store.webhook_forward_url()
    if forward:
        _spawn_forward(forward)

    # Claim the ring synchronously so duplicate reports are rejected before we
    # answer, but play in the background: streaming a chime takes as long as
    # the clip itself, and the door station's HTTP favourite must not be left
    # hanging for that. It fires and forgets.
    if not ring_service.claim_ring(device_id):
        log.info("[device %s] ring via webhook ignored (debounce)", device_id)
        return {"ok": False, "detail": "ignored (debounce)"}

    log.info("[device %s] RING via webhook", device_id)
    asyncio.get_running_loop().run_in_executor(
        None, ring_service.play_active_chime, device_id
    )
    return {"ok": True, "detail": "ring accepted; playing chime"}


# Tasks are held here for their lifetime. `asyncio.create_task` returns a
# future the event loop only holds a *weak* reference to, so a task nobody
# keeps can be garbage-collected before it ever runs -- intermittently, under
# load, and impossible to reproduce on demand. This is the documented fix.
_pending_forwards: set[asyncio.Task] = set()


def _spawn_forward(url: str) -> asyncio.Task:
    task = asyncio.create_task(_forward(url))
    _pending_forwards.add(task)
    task.add_done_callback(_pending_forwards.discard)
    return task


async def _forward(url: str) -> None:
    """Pass the ring on to the integration this app displaced. Best-effort.

    TLS is verified here, unlike the door-station calls: this URL is whatever
    the admin typed and may well point off the LAN, so the reason the device
    connections skip verification (self-signed certs on a known local box)
    does not apply.
    """
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            r = await client.get(url)
        log.info("forwarded ring -> %s (HTTP %s)", _redact(url), r.status_code)
    except Exception as exc:
        log.warning("forwarding ring to %s failed: %s", _redact(url), exc)


def _redact(url: str) -> str:
    """Hide any user:password@ embedded in a forward URL before logging it."""
    parsed = urlsplit(url)
    if parsed.username:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, f"***@{host}", parsed.path, parsed.query, ""))
    return url


def _tokens_match(supplied: str, expected: str) -> bool:
    import secrets
    return secrets.compare_digest(supplied, expected)
