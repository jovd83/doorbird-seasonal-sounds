"""MP3 -> G.711 mu-law transcoding for the door station's speaker.

`audio-transmit.cgi` only accepts G.711 mu-law, 8 kHz, mono (see the LAN API,
"LIVE AUDIO RECEIVE AND TRANSMIT"). That's telephone-band, so every MP3 in the
library has to be converted before it can be played at the door.

Conversion is cached on disk under `<data>/ulaw/`. The cache key includes the
source file's size and mtime, so replacing an MP3 in place invalidates it
without anyone having to remember to clear anything.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from app.config import settings

log = logging.getLogger("doorbird.audio")

ULAW_RATE = 8000          # bytes per second: mu-law 8 kHz mono is 1 byte/sample
FFMPEG_TIMEOUT = 60.0


class TranscodeError(Exception):
    pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _cache_name(src: Path) -> str:
    st = src.stat()
    stem = src.stem
    return f"{stem}-{st.st_size}-{int(st.st_mtime)}-{int(settings.chime_gain_db)}db.ulaw"


def _build_filters() -> str | None:
    """Optional post-processing applied before mu-law encoding.

    The door speaker is small and the band is narrow, so a gain trim is the
    one knob that actually matters in practice. Kept opt-in: with the default
    of 0 dB no filter is inserted at all and the conversion stays lossless
    apart from the resample.
    """
    filters: list[str] = []
    if settings.chime_gain_db:
        filters.append(f"volume={settings.chime_gain_db}dB")
    return ",".join(filters) if filters else None


def ensure_ulaw(mp3_path: Path) -> Path:
    """Return a cached mu-law rendering of `mp3_path`, transcoding on demand."""
    if not mp3_path.exists():
        raise TranscodeError(f"source MP3 missing: {mp3_path}")
    if not ffmpeg_available():
        raise TranscodeError(
            "ffmpeg is not installed in this container - rebuild the image "
            "(the Dockerfile installs it) or `apt-get install ffmpeg`"
        )

    out_dir = settings.ulaw_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / _cache_name(mp3_path)
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    tmp = dest.with_suffix(".ulaw.part")
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(mp3_path)]
    filters = _build_filters()
    if filters:
        cmd += ["-af", filters]
    if settings.chime_max_seconds > 0:
        cmd += ["-t", str(settings.chime_max_seconds)]
    cmd += ["-ar", str(ULAW_RATE), "-ac", "1", "-f", "mulaw", str(tmp)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        tmp.unlink(missing_ok=True)
        raise TranscodeError(f"ffmpeg timed out converting {mp3_path.name}") from exc

    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise TranscodeError(
            f"ffmpeg failed on {mp3_path.name}: {(proc.stderr or '').strip()[:300]}"
        )

    tmp.replace(dest)
    log.info("transcoded %s -> %s (%.2fs of audio)",
             mp3_path.name, dest.name, dest.stat().st_size / ULAW_RATE)
    return dest


def ulaw_duration_seconds(ulaw_path: Path) -> float:
    return ulaw_path.stat().st_size / ULAW_RATE


def purge_cache() -> int:
    """Delete every cached rendering. Returns how many files were removed."""
    if not settings.ulaw_dir.exists():
        return 0
    removed = 0
    for f in settings.ulaw_dir.glob("*.ulaw"):
        f.unlink(missing_ok=True)
        removed += 1
    return removed
