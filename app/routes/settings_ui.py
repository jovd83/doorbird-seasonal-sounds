"""Settings page: choose how a ring reaches this app."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import ring, settings_store
from app.config import settings as env_settings
from app.db import get_db
from app.models import Device
from app.security import require_auth, require_csrf
from app.shell import resolve_shell
from app.templating import templates

router = APIRouter(
    prefix="/settings",
    dependencies=[Depends(require_auth), Depends(require_csrf), Depends(resolve_shell)],
)


def _webhook_base(request: Request, snap=None) -> tuple[str, bool]:
    """Return (base URL for generated links, whether it was configured).

    Prefers the admin-configured public base URL. Falls back to whatever the
    browser used, which is only right when the admin happens to be browsing
    via the same address another machine would dial — not the case when this
    runs in a container reached over a published port, or behind a proxy.
    """
    configured = settings_store.public_base_url(snap)
    if configured:
        return configured, True
    return str(request.base_url).rstrip("/"), False


@router.get("")
async def settings_page(request: Request, db: Session = Depends(get_db)):
    devices = db.query(Device).order_by(Device.name).all()
    # One snapshot for the whole page: this used to be ten separate sessions.
    snap = settings_store.snapshot()
    token = settings_store.webhook_token(snap)
    base, base_configured = _webhook_base(request, snap)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "modes": settings_store.TRIGGER_MODES,
            "current_mode": settings_store.trigger_mode(snap),
            "ring_chime_enabled": env_settings.ring_chime_enabled,
            "devices": devices,
            "webhook_base": base,
            "base_configured": base_configured,
            "base_unreachable": settings_store.looks_unreachable(base),
            "webhook_token": token,
            "webhook_urls": [
                (d.name, f"{base}/ring/{token}?device={d.id}") for d in devices
            ],
            "forward_url": settings_store.webhook_forward_url(snap),
            "play_default_when_idle": settings_store.play_default_when_idle(snap),
            "current_theme": settings_store.theme(snap),
            "ring_status": ring.status(),
        },
    )


@router.post("/trigger-mode")
async def set_trigger_mode(mode: str = Form(...)):
    try:
        settings_store.set_trigger_mode(mode)
    except ValueError:
        return RedirectResponse("/settings?warn=unknown+trigger+mode", status_code=303)

    # Starting/stopping listener threads is the whole point of this switch.
    ring.refresh()
    ring.start()
    label = settings_store.TRIGGER_MODES[mode]
    return RedirectResponse(f"/settings?msg=Trigger+set+to:+{label}", status_code=303)


@router.post("/rotate-token")
async def rotate_token():
    settings_store.rotate_webhook_token()
    return RedirectResponse(
        "/settings?warn=Webhook+URL+changed+-+update+it+on+every+DoorBird",
        status_code=303,
    )


@router.post("/forward-url")
async def set_forward_url(forward_url: str = Form("")):
    try:
        settings_store.set_webhook_forward_url(forward_url)
    except ValueError as exc:
        return RedirectResponse(
            f"/settings?warn={quote(str(exc), safe='')}", status_code=303)
    note = "Ring will be forwarded on" if forward_url.strip() else "Forwarding disabled"
    return RedirectResponse(f"/settings?msg={quote(note, safe='')}", status_code=303)


@router.post("/base-url")
async def set_base_url(base_url: str = Form("")):
    """Pin the address other systems use to reach this app."""
    cleaned = (base_url or "").strip()
    if cleaned and not cleaned.startswith(("http://", "https://")):
        cleaned = f"http://{cleaned}"
    settings_store.set_public_base_url(cleaned)
    if cleaned and settings_store.looks_unreachable(cleaned):
        return RedirectResponse(
            "/settings?warn=That+address+is+local+to+this+machine+-+other+systems+cannot+reach+it",
            status_code=303,
        )
    note = "Base+URL+saved" if cleaned else "Base+URL+cleared+-+falling+back+to+auto-detect"
    return RedirectResponse(f"/settings?msg={note}", status_code=303)


@router.post("/play-default")
async def set_play_default(play_default: bool = Form(False)):
    """Toggle whether the default MP3 plays when no schedule matches."""
    settings_store.set_play_default_when_idle(play_default)
    note = ("Default+will+play+when+no+schedule+is+active" if play_default
            else "Silent+when+no+schedule+is+active")
    return RedirectResponse(f"/settings?msg={note}", status_code=303)


@router.post("/theme")
async def set_theme(theme: str = Form(settings_store.THEME_DARK)):
    """Switch the web UI between dark and light.

    Posted from a switch that has already flipped the attribute on the client,
    so the redirect lands on a page that is already the right colour.
    """
    try:
        settings_store.set_theme(theme)
    except ValueError:
        return RedirectResponse("/settings?warn=unknown+theme", status_code=303)
    label = "Light" if theme == settings_store.THEME_LIGHT else "Dark"
    return RedirectResponse(f"/settings?msg={label}+mode+on", status_code=303)
