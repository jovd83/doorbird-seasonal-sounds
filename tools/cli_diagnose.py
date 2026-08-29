"""One-shot capability report for a configured DoorBird.

Answers, for a given device, the only questions that actually matter when
this app misbehaves:

  1. Do the credentials work at all?              (info.cgi)
  2. Can we hear ring events?                     (monitor.cgi)
  3. Can we play audio out of the door speaker?   (audio-transmit.cgi, silent)
  4. Is the button-sound endpoint reachable       (customsound.cgi)
     for *these* credentials?

Step 3 posts G.711 mu-law silence, so it is inaudible at the door.

Credentials are read from the encrypted DB and never printed.

Usage:
    docker exec doorbird-seasonal python -m tools.cli_diagnose front-door
"""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from app.crypto import decrypt
from app.db import SessionLocal
from app.doorbird import DeviceCreds, DoorBirdClient, DoorBirdError
from app.models import Device

OK, BAD, MEH = "OK  ", "FAIL", "??  "


def _line(tag: str, label: str, detail: str = "") -> None:
    print(f"  [{tag}] {label}" + (f" — {detail}" if detail else ""))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m tools.cli_diagnose <device-name>", file=sys.stderr)
        return 2

    with SessionLocal() as db:
        d = db.query(Device).filter(Device.name == argv[1]).first()
        if not d:
            names = [x.name for x in db.query(Device).all()]
            print(f"no device named {argv[1]!r}. Available: {names}", file=sys.stderr)
            return 1
        creds = DeviceCreds(
            host=d.host, username=d.username,
            password=decrypt(d.password_enc), use_https=d.use_https,
        )
        name = d.name

    print(f"\nDiagnosing {name} at {creds.clean_host}\n")
    failures = 0

    # 1 -------------------------------------------------------------------
    print("1. Credentials / reachability")
    with DoorBirdClient(creds) as client:
        ok, msg = client.test_connection()
    _line(OK if ok else BAD, "info.cgi", msg)
    failures += 0 if ok else 1

    # 2 -------------------------------------------------------------------
    print("\n2. Ring events (needed to trigger the chime)")
    seen: list[str] = []
    try:
        with DoorBirdClient(creds) as client:
            # The device sends one snapshot line per requested input and then
            # goes quiet until something happens, so the read timeout firing
            # after we have a snapshot is the healthy outcome, not an error.
            for event, state in client.stream_ring_events(
                timeout=6.0, event_types="doorbell,motionsensor"
            ):
                seen.append(f"{event}:{state}")
                if len(seen) >= 2:
                    break
    except (DoorBirdError, httpx.HTTPError) as exc:
        if not seen:
            _line(BAD, "monitor.cgi", str(exc)[:160])
            failures += 1

    if seen:
        _line(OK, "monitor.cgi", f"streaming — current state {', '.join(seen)}")

    # 3 -------------------------------------------------------------------
    print("\n3. Speaker playback (silent test — nothing audible at the door)")
    tmp = None
    try:
        with NamedTemporaryFile(suffix=".ulaw", delete=False) as fh:
            fh.write(b"\xff" * 8000)          # 1 s of mu-law silence
            tmp = Path(fh.name)
        with DoorBirdClient(creds) as client:
            _line(OK, "audio-transmit.cgi", client.play_ulaw(tmp))
    except DoorBirdError as exc:
        _line(BAD, "audio-transmit.cgi", str(exc)[:200])
        failures += 1
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)

    # 4 -------------------------------------------------------------------
    print("\n4. Button-sound upload (the original, still-blocked approach)")
    with DoorBirdClient(creds) as client:
        rows = client.diagnose_button_sound()
    for row in rows:
        tag = MEH
        note = ""
        if row.path.endswith("nonexistent-control-probe.cgi"):
            note = "control: a path that does not exist"
            tag = OK if row.basic == 404 else MEH
        elif row.basic == 200:
            tag, note = OK, "reachable for these credentials!"
        elif row.basic == 401:
            tag, note = MEH, "exists but refuses this account"
        _line(tag, f"{row.path:<42} basic={row.basic} digest={row.digest}", note)

    cs = next((r for r in rows if r.path.endswith("customsound.cgi")), None)
    if cs is not None and cs.basic == 401:
        print("\n  customsound.cgi answers 401 while a bogus path answers 404, so the")
        print("  endpoint is real but gated to the factory administration account")
        print("  (the ...0000 user from the DoorBird Digital Passport). Ring-chime")
        print("  mode does not need it.")
    elif cs.get("basic") not in (401, None):
        print("\n  customsound.cgi did NOT return 401 for this account — these")
        print("  credentials may be privileged enough to drive a real upload.")

    print(f"\nSummary: {'all core checks passed' if failures == 0 else f'{failures} core check(s) failed'}")
    print("Ring-chime mode needs checks 1-3 only.\n")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
