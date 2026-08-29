"""Probe a single DoorBird endpoint with several method/body combos.

Usage:
    docker exec doorbird-seasonal python -m tools.cli_deep_probe front-door /bha-api/customsound.cgi
"""
from __future__ import annotations

import subprocess
import sys

from app.crypto import decrypt
from app.db import SessionLocal
from app.models import Device


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python -m tools.cli_deep_probe <device-name> <path>", file=sys.stderr)
        return 2

    name, path = argv[1], argv[2]
    with SessionLocal() as db:
        d = db.query(Device).filter(Device.name == name).first()
        if not d:
            print(f"no device {name!r}", file=sys.stderr)
            return 1
        user = d.username
        pwd = decrypt(d.password_enc)
        scheme = "https" if d.use_https else "http"
        url = f"{scheme}://{d.host}{path}"

    cases = [
        # name,           method, extra curl args,                   note
        ("GET (plain)",          "GET",  [],                                          ""),
        ("GET ?action=list",     "GET",  [],                                          "?action=list"),
        ("GET ?action=help",     "GET",  [],                                          "?action=help"),
        ("HEAD",                 "HEAD", ["-I"],                                       ""),
        ("OPTIONS",              "OPTIONS", ["-X", "OPTIONS"],                         ""),
        ("POST empty",           "POST", ["-X", "POST", "--data-binary", ""],          ""),
        ("POST raw audio/mpeg",  "POST", ["-X", "POST",
                                          "-H", "Content-Type: audio/mpeg;charset=UTF-8",
                                          "--data-binary", "@/dev/null"],              ""),
        ("POST JSON buttonSound=custom", "POST", ["-X", "POST",
                                          "-H", "Content-Type: application/json",
                                          "--data", '{"buttonSound":"custom"}'],       ""),
        ("POST ?action=save",    "POST", ["-X", "POST"],                              "?action=save"),
    ]

    print(f"deep probe of {url}\n")
    for label, _method, extra, suffix in cases:
        full = url + suffix
        cmd = [
            "curl", "-sk", "-m", "5",
            "-o", "-",
            "-w", "\n=== HTTP %{http_code} (time=%{time_total}s) ===\n",
            "-u", f"{user}:{pwd}",
            *extra, full,
        ]
        print(f"\n--- {label}  ---")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            out = (proc.stdout or "").strip()
            # Truncate huge bodies
            if len(out) > 600:
                out = out[:600] + "…(truncated)"
            print(out)
            if proc.returncode != 0 and proc.returncode != 22:
                print(f"  (curl exit={proc.returncode})")
        except subprocess.TimeoutExpired:
            print("  TIMEOUT")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
