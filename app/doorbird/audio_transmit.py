"""Streaming G.711 mu-law out of a door station's speaker.

This is a hand-written HTTP/1.0 exchange on a raw socket, not an httpx call,
and that is deliberate: `audio-transmit.cgi` wants an explicit Content-Length
on an HTTP/1.0 request, and the body has to be paced at the audio's own rate of
8000 bytes per second. Pushing it as fast as the LAN allows overruns the
device's buffer -- DoorBird's own curl example pins this with `--limit-rate 8K`.

Split out of the HTTP client because it shares nothing with it but the
credentials: different protocol version, different transport, different failure
modes. Keeping them together meant no part of either could be read or tested
without the other.
"""
from __future__ import annotations

import base64
import contextlib
import logging
import socket
import time
from pathlib import Path

from app.doorbird.types import (
    CONNECT_TIMEOUT,
    READ_TIMEOUT,
    DeviceCreds,
    DoorBirdError,
)

log = logging.getLogger("doorbird.audio")

# mu-law 8 kHz mono is one byte per sample, so bytes-per-second == sample rate.
ULAW_BYTES_PER_SECOND = 8000
# 100 ms of audio per write. Small enough to pace smoothly, large enough that
# the syscall count stays irrelevant.
CHUNK_BYTES = 800

RETRY_ON = 503
DEFAULT_RETRIES = 3
DEFAULT_RETRY_WAIT = 1.5


def play_ulaw(
    creds: DeviceCreds,
    ulaw_path: Path,
    *,
    retries: int = DEFAULT_RETRIES,
    retry_wait: float = DEFAULT_RETRY_WAIT,
) -> str:
    """Stream a mu-law file out of the door speaker, retrying while busy.

    Retries on 503: the door station allows only one audio consumer at a time,
    so a phone that has just opened live view for the ring owns the channel and
    we get "Service Not Available". That window is usually short, and giving up
    after a few seconds is right -- by then either the channel freed up, or
    somebody is genuinely talking to the visitor and a chime would interrupt.
    """
    last: DoorBirdError | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            return _play_once(creds, ulaw_path)
        except DoorBirdError as exc:
            last = exc
            if str(RETRY_ON) not in str(exc) or attempt == retries:
                raise
            log.info("audio-transmit busy (503), retry %d/%d", attempt, retries - 1)
            time.sleep(retry_wait)
    raise last  # pragma: no cover - the loop always returns or raises


def _play_once(creds: DeviceCreds, ulaw_path: Path) -> str:
    """One attempt at the transmit described in `play_ulaw`."""
    data = ulaw_path.read_bytes()
    if not data:
        raise DoorBirdError(f"empty audio file: {ulaw_path}")

    host = creds.clean_host
    port = 443 if creds.use_https else 80
    duration = len(data) / ULAW_BYTES_PER_SECOND

    sock: socket.socket | None = None
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        if creds.use_https:
            sock = _wrap_tls(sock, host, verify=creds.verify_tls)
        # The write must outlast the audio itself, plus room for the reply.
        sock.settimeout(duration + READ_TIMEOUT)
        sock.sendall(_request_head(creds, host, len(data)))
        _send_paced(sock, data)
        status_line = _read_status_line(sock)
    except OSError as exc:
        raise DoorBirdError(f"audio-transmit.cgi socket error: {exc}") from exc
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    _raise_for_status(status_line)
    return f"played {ulaw_path.name} ({duration:.1f}s) through the door speaker"


def _request_head(creds: DeviceCreds, host: str, length: int) -> bytes:
    token = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
    return (
        "POST /bha-api/audio-transmit.cgi HTTP/1.0\r\n"
        f"Host: {host}\r\n"
        f"Authorization: Basic {token}\r\n"
        "Content-Type: audio/basic\r\n"
        f"Content-Length: {length}\r\n"
        "Connection: Keep-Alive\r\n"
        "Cache-Control: no-cache\r\n\r\n"
    ).encode()


def _wrap_tls(sock: socket.socket, host: str, *, verify: bool = False) -> socket.socket:
    """TLS for the transmit socket, verified only if the device says so.

    Door stations ship self-signed certificates and are addressed by IP, so
    verification cannot succeed on a stock device -- but it is the device's
    setting to make, not a hardcoded decision. Unrelated to the outbound
    webhook relay, which always verifies.
    """
    import ssl

    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(sock, server_hostname=host)


def _send_paced(sock: socket.socket, data: bytes) -> None:
    """Write the body at the audio's own rate, not the link's."""
    started = time.monotonic()
    for offset in range(0, len(data), CHUNK_BYTES):
        sock.sendall(data[offset:offset + CHUNK_BYTES])
        sent_seconds = (offset + CHUNK_BYTES) / ULAW_BYTES_PER_SECOND
        ahead = started + sent_seconds - time.monotonic()
        if ahead > 0:
            time.sleep(ahead)


def _read_status_line(sock: socket.socket) -> str:
    buf = b""
    try:
        while b"\r\n" not in buf and len(buf) < 4096:
            block = sock.recv(1024)
            if not block:
                break
            buf += block
    except OSError:
        pass
    return buf.split(b"\r\n")[0].decode("latin-1", errors="replace")


def status_code(status_line: str) -> int | None:
    parts = status_line.split()
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return None


def _raise_for_status(status_line: str) -> None:
    """Translate the device's reply into something a user can act on."""
    code = status_code(status_line)
    if code == 200:
        return
    if code == 401:
        raise DoorBirdError("401 on audio-transmit.cgi — credentials rejected")
    if code == 204:
        raise DoorBirdError(
            "204 on audio-transmit.cgi — this user may not use live audio right now. "
            "Enable 'Watch always' for it in the DoorBird app "
            "(Settings → Administration → User), or accept that playback only "
            "works within 5 minutes of a ring."
        )
    if code == 503:
        raise DoorBirdError(
            "503 on audio-transmit.cgi — the door station's audio channel is already "
            "in use (someone has live view or talk open). Only one consumer at a time."
        )
    raise DoorBirdError(f"audio-transmit.cgi returned {status_line or 'no response'}")
