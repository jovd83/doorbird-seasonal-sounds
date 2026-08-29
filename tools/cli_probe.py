"""CLI probe runner — bypasses the web UI completely.

Usage (from the host):
    docker exec doorbird-seasonal python -m tools.cli_probe <device-name>

Looks the device up in the SQLite DB, decrypts its password, runs the same
probe_endpoints() the UI button runs, and prints the results to stdout.
"""
from __future__ import annotations

import sys

from app.crypto import decrypt
from app.db import SessionLocal
from app.doorbird import DeviceCreds, DoorBirdClient
from app.models import Device


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m tools.cli_probe <device-name>", file=sys.stderr)
        return 2

    name = argv[1]
    with SessionLocal() as db:
        d = db.query(Device).filter(Device.name == name).first()
        if not d:
            names = [x.name for x in db.query(Device).all()]
            print(f"no device named {name!r}. Available: {names}", file=sys.stderr)
            return 1
        creds = DeviceCreds(
            host=d.host, username=d.username,
            password=decrypt(d.password_enc), use_https=d.use_https,
        )

    print(f"probing {d.name} at {creds.clean_host} ...", file=sys.stderr)
    with DoorBirdClient(creds) as client:
        results = client.probe_endpoints()

    print(f"{'PATH':<44} {'GET':>10} {'POST':>10}  excerpt")
    print("-" * 100)
    for r in results:
        get = str(r.get("GET", "—"))
        post = str(r.get("POST", "—"))
        excerpt = (r.get("GET_excerpt") or "").replace("\n", " ")[:60]
        print(f"{r['path']:<44} {get:>10} {post:>10}  {excerpt}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
