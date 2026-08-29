"""Storing and promoting MP3s — the parts two different routes both needed.

`routes/mp3s.py` and `routes/schedules.py` each carried their own copy of the
same sequence: check the extension, invent a filename, write the bytes,
inspect, roll back on failure, check the label, insert. Two near-identical
forty-line blocks that had to be kept in step by hand, and only one of which
ever grew a fix. They both call in here now.

The write itself is bounded and streamed. The previous version did
`target.write_bytes(await file.read())`, which pulled the whole upload into
memory before anything had looked at it; `DOORBIRD_MAX_BYTES` existed but only
ever appended an advisory note to the validation issues, so a multi-gigabyte
POST was accepted, buffered in RAM, and written to the NAS volume in full.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KIND_CHIME, KINDS, Mp3File
from app.mp3_validator import inspect_mp3

log = logging.getLogger("doorbird.library")

# 64 KiB: large enough that the syscall overhead is irrelevant, small enough
# that an oversized upload is refused long before it costs real memory.
CHUNK_BYTES = 64 * 1024


class UploadTooLarge(HTTPException):
    def __init__(self, limit: int):
        super().__init__(
            413,
            f"That file is larger than the {limit // 1024 // 1024} MB limit. "
            "Door chimes are a few seconds long — trim it, or lower the bitrate.",
        )


def _stream_to_disk(upload: UploadFile, target: Path, max_bytes: int) -> None:
    """Copy an upload to `target` in chunks, refusing to exceed `max_bytes`.

    The partial file is removed on any failure, so a rejected upload never
    leaves a fragment behind for the next reader to trip over.
    """
    written = 0
    try:
        with target.open("wb") as out:
            while True:
                chunk = upload.file.read(CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise UploadTooLarge(max_bytes)
                out.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise

    if written == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "That file is empty.")


def store_uploaded_mp3(
    db: Session,
    *,
    upload: UploadFile,
    label: str,
    kind: str = KIND_CHIME,
    is_default: bool = False,
) -> Mp3File:
    """Validate, store and register one uploaded MP3. Returns the new row.

    Raises `HTTPException` with a message meant for the person who submitted
    the form. The caller is responsible for the surrounding transaction; on any
    raise, nothing has been added to the session and no file remains on disk.
    """
    if kind not in KINDS:
        raise HTTPException(400, f"unknown MP3 type {kind!r}")
    if is_default and kind != KIND_CHIME:
        raise HTTPException(400, "only a chime can be the default sound")

    filename = (upload.filename or "").strip()
    if not filename.lower().endswith(".mp3"):
        raise HTTPException(400, "file must be an .mp3")

    clean_label = (label or "").strip()
    if not clean_label:
        raise HTTPException(400, "give the uploaded MP3 a label")
    if db.query(Mp3File).filter(Mp3File.label == clean_label).first():
        raise HTTPException(400, f"MP3 label {clean_label!r} already exists")

    stored_name = f"{uuid.uuid4().hex}.mp3"
    target: Path = settings.mp3_dir / stored_name
    target.parent.mkdir(parents=True, exist_ok=True)

    _stream_to_disk(upload, target, settings.max_upload_bytes)

    try:
        info = inspect_mp3(target)
    except ValueError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc

    if is_default:
        _clear_default(db)

    record = Mp3File(
        label=clean_label,
        filename=stored_name,
        size_bytes=info.size_bytes,
        duration_seconds=info.duration_seconds,
        sample_rate_hz=info.sample_rate_hz,
        bitrate_kbps=info.bitrate_kbps,
        kind=kind,
        is_default=is_default,
    )
    db.add(record)
    db.flush()
    log.info("stored MP3 %r as %s (%d bytes, kind=%s)",
             clean_label, stored_name, info.size_bytes, kind)
    return record


def _clear_default(db: Session) -> None:
    for existing in db.query(Mp3File).filter(Mp3File.is_default.is_(True)).all():
        existing.is_default = False


def make_default(db: Session, mp3: Mp3File) -> None:
    """Promote one chime to the default, demoting whatever held it.

    Only chimes are eligible: the default is what plays when no schedule
    matches, and a spoken delivery message in that slot would be wrong.
    """
    if mp3.kind != KIND_CHIME:
        raise HTTPException(400, "only a chime can be the default sound")
    _clear_default(db)
    mp3.is_default = True


def delete_stored_file(mp3: Mp3File) -> None:
    """Remove an MP3's bytes from disk. Safe to call when already gone."""
    (settings.mp3_dir / mp3.filename).unlink(missing_ok=True)
