"""Door station CRUD, plus the buttons that talk to a device.

Every route that reaches out to a door station does so through
`run_in_threadpool`. `DoorBirdClient` is synchronous and a single unreachable
device costs 6 s to connect plus up to 30 s to read; called directly from a
coroutine that would block the one event loop this app runs on, freezing every
other request -- including the ring webhook -- for the whole of that time.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import ring
from app.crypto import decrypt, encrypt
from app.db import get_db, session_scope
from app.doorbird import DeviceCreds, DoorBirdClient
from app.engine import apply_to_device, today_resolution
from app.models import Device
from app.scheduler import trigger_on_change
from app.security import require_auth, require_csrf
from app.shell import resolve_shell
from app.templating import templates

router = APIRouter(
    prefix="/devices",
    dependencies=[Depends(require_auth), Depends(require_csrf), Depends(resolve_shell)],
)


def _creds_for(device: Device) -> DeviceCreds:
    return DeviceCreds(
        host=device.host,
        username=device.username,
        password=decrypt(device.password_enc),
        use_https=device.use_https,
        verify_tls=device.verify_tls,
    )


@router.get("")
async def list_devices(request: Request, db: Session = Depends(get_db)):
    devices = db.query(Device).order_by(Device.name).all()
    return templates.TemplateResponse(request, "devices.html", {"devices": devices})


@router.post("/create")
async def create_device(
    name: str = Form(...),
    host: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    use_https: bool = Form(False),
    verify_tls: bool = Form(False),
    enabled: bool = Form(True),
):
    with session_scope() as db:
        if db.query(Device).filter(Device.name == name).first():
            raise HTTPException(400, f"Device named {name!r} already exists")
        db.add(Device(
            name=name.strip(),
            host=host.strip(),
            username=username.strip(),
            password_enc=encrypt(password),
            use_https=use_https,
            verify_tls=verify_tls,
            enabled=enabled,
        ))
    trigger_on_change()
    ring.refresh()
    return RedirectResponse("/devices", status_code=303)


@router.post("/{device_id}/update")
async def update_device(
    device_id: int,
    name: str = Form(...),
    host: str = Form(...),
    username: str = Form(...),
    password: str = Form(""),
    use_https: bool = Form(False),
    verify_tls: bool = Form(False),
    enabled: bool = Form(True),
):
    with session_scope() as db:
        d = db.get(Device, device_id)
        if not d:
            raise HTTPException(404, "device not found")
        d.name = name.strip()
        d.host = host.strip()
        d.username = username.strip()
        if password:
            d.password_enc = encrypt(password)
        d.use_https = use_https
        d.verify_tls = verify_tls
        d.enabled = enabled
    trigger_on_change()
    # `refresh` compares each running watcher's credentials against the row it
    # was built from, so an edit here actually reaches the listener thread.
    ring.refresh()
    return RedirectResponse("/devices", status_code=303)


@router.post("/{device_id}/delete")
async def delete_device(device_id: int):
    with session_scope() as db:
        d = db.get(Device, device_id)
        if d:
            db.delete(d)
    ring.refresh()
    return RedirectResponse("/devices", status_code=303)


@router.post("/{device_id}/test")
async def test_device(device_id: int, db: Session = Depends(get_db)):
    d = db.get(Device, device_id)
    if not d:
        raise HTTPException(404, "device not found")
    creds = _creds_for(d)

    def _run() -> tuple[bool, str]:
        with DoorBirdClient(creds) as client:
            return client.test_connection()

    ok, msg = await run_in_threadpool(_run)

    d.last_error = None if ok else msg
    db.commit()
    return RedirectResponse(f"/devices?msg={'OK: ' if ok else 'FAIL: '}{msg}", status_code=303)


@router.post("/{device_id}/probe")
async def probe_device(device_id: int, request: Request, db: Session = Depends(get_db)):
    d = db.get(Device, device_id)
    if not d:
        raise HTTPException(404, "device not found")
    creds = _creds_for(d)

    def _run():
        with DoorBirdClient(creds) as client:
            return client.probe_endpoints()

    results = await run_in_threadpool(_run)
    return templates.TemplateResponse(
        request, "probe.html",
        {"device": d, "results": results},
    )


@router.post("/{device_id}/apply")
async def apply_device_now(device_id: int):
    def _run() -> None:
        with session_scope() as db:
            d = db.get(Device, device_id)
            if not d:
                raise HTTPException(404, "device not found")
            res = today_resolution(db)
            if res is None:
                raise HTTPException(
                    400, "no default MP3 set — upload one and mark it default first")
            apply_to_device(db, d, res.mp3, schedule=res.schedule, force=True)

    await run_in_threadpool(_run)
    return RedirectResponse("/devices", status_code=303)


@router.post("/{device_id}/chime")
async def chime_device_now(device_id: int):
    """Play today's active sound through this door station's speaker, now.

    This makes real noise at the front door, so it is only ever reachable from
    an explicit button press in the UI.
    """
    ok, msg = await run_in_threadpool(ring.play_active_chime, device_id)
    if not ok:
        raise HTTPException(400, msg)
    return RedirectResponse("/devices", status_code=303)
