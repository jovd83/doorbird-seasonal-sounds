"""Everything that talks to a door station.

Split out of a single 558-line module that mixed four unrelated jobs: an httpx
client, a hand-written HTTP/1.0 raw-socket audio transmit, a curl-subprocess
endpoint scanner, and auth diagnostics. Any change to transport risked the
discovery code and vice versa, and none of it could be tested without a socket.

    types            credentials, errors, result records — no transport
    client           the HTTP client (info, upload, monitor stream)
    audio_transmit   the raw-socket mu-law transmit
    diagnostics      endpoint discovery and auth probing (development tooling)
"""
from app.doorbird.client import DoorBirdClient
from app.doorbird.types import (
    AuthDiagnostic,
    DeviceCreds,
    DoorBirdError,
    ProbeResult,
)

__all__ = [
    "AuthDiagnostic",
    "DeviceCreds",
    "DoorBirdClient",
    "DoorBirdError",
    "ProbeResult",
]
