from dataclasses import dataclass
from pathlib import Path

from mutagen.mp3 import MP3, HeaderNotFoundError

DOORBIRD_ALLOWED_SAMPLE_RATES = (44100, 48000)
DOORBIRD_MAX_DURATION_S = 10.0
DOORBIRD_MAX_BYTES = 2 * 1024 * 1024  # 2 MB safety cap


@dataclass
class Mp3Info:
    duration_seconds: float
    sample_rate_hz: int
    bitrate_kbps: int
    issues: list[str]
    size_bytes: int

    @property
    def ok(self) -> bool:
        return not self.issues


def inspect_mp3(path: Path) -> Mp3Info:
    size = path.stat().st_size
    issues: list[str] = []
    try:
        audio = MP3(path)
    except HeaderNotFoundError as exc:
        raise ValueError(f"Not a valid MP3 file: {exc}") from exc

    sr = int(getattr(audio.info, "sample_rate", 0) or 0)
    dur = float(getattr(audio.info, "length", 0.0) or 0.0)
    br = int((getattr(audio.info, "bitrate", 0) or 0) // 1000)

    if sr not in DOORBIRD_ALLOWED_SAMPLE_RATES:
        issues.append(f"sample_rate={sr}Hz (DoorBird requires 44100 or 48000)")
    if dur > DOORBIRD_MAX_DURATION_S:
        issues.append(f"duration={dur:.2f}s (DoorBird allows max {DOORBIRD_MAX_DURATION_S:.0f}s)")
    if size > DOORBIRD_MAX_BYTES:
        issues.append(f"size={size} bytes (>2 MB safety cap)")

    return Mp3Info(
        duration_seconds=dur,
        sample_rate_hz=sr,
        bitrate_kbps=br,
        size_bytes=size,
        issues=issues,
    )
