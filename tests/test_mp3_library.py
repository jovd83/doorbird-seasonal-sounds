"""Upload storage: the size cap, the rollback, and the shared code path.

The cap is the point of this file. `DOORBIRD_MAX_BYTES` existed before but only
ever added an advisory note to the validation output, so an arbitrarily large
POST was read into memory in full, written to the data volume, and accepted.
"""
from __future__ import annotations

import io

import pytest
from fastapi import HTTPException, UploadFile

from app.config import settings
from app.db import init_db, session_scope
from app.models import KIND_AUTO_RESPONSE, KIND_CHIME, Mp3File
from app.mp3_library import store_uploaded_mp3

# Structurally valid silence: MPEG-1 Layer III, 128 kbps, 44.1 kHz, 417 B/frame.
_FRAME = b"\xff\xfb\x90\xc0" + b"\x00" * 413
SILENT_MP3 = _FRAME * 40


def _upload(data: bytes, name: str = "chime.mp3") -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data))


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    with session_scope() as db:
        db.query(Mp3File).delete()
    yield
    with session_scope() as db:
        db.query(Mp3File).delete()


def _mp3_files_on_disk() -> set[str]:
    return {p.name for p in settings.mp3_dir.glob("*.mp3")}


def test_a_normal_upload_is_stored():
    with session_scope() as db:
        record = store_uploaded_mp3(db, upload=_upload(SILENT_MP3), label="Bells")
        assert record.label == "Bells"
        assert record.kind == KIND_CHIME
        assert record.size_bytes == len(SILENT_MP3)
        assert (settings.mp3_dir / record.filename).exists()


def test_an_oversized_upload_is_refused():
    """The cap must reject, not merely annotate."""
    before = _mp3_files_on_disk()
    huge = SILENT_MP3 + b"\x00" * (settings.max_upload_bytes + 1)

    with session_scope() as db, pytest.raises(HTTPException) as caught:
        store_uploaded_mp3(db, upload=_upload(huge), label="Enormous")

    assert caught.value.status_code == 413
    # And nothing was left behind on the data volume.
    assert _mp3_files_on_disk() == before


def test_an_oversized_upload_is_stopped_before_it_is_all_read():
    """Proof the cap is applied while streaming, not after buffering.

    The stream raises if it is read past the limit, so reaching the exception
    at all means the writer stopped early.
    """
    limit = settings.max_upload_bytes

    class _Tripwire(io.BytesIO):
        def read(self, size=-1):
            if self.tell() > limit + (1024 * 1024):
                raise AssertionError("kept reading well past the size limit")
            return super().read(size)

    payload = SILENT_MP3 + b"\x00" * (limit * 4)
    upload = UploadFile(filename="huge.mp3", file=_Tripwire(payload))

    with session_scope() as db, pytest.raises(HTTPException) as caught:
        store_uploaded_mp3(db, upload=upload, label="Streamed")
    assert caught.value.status_code == 413


def test_a_non_mp3_is_refused_by_extension():
    with session_scope() as db, pytest.raises(HTTPException) as caught:
        store_uploaded_mp3(db, upload=_upload(SILENT_MP3, "notes.txt"), label="Text")
    assert caught.value.status_code == 400


def test_garbage_content_is_refused_and_rolled_back():
    before = _mp3_files_on_disk()
    with session_scope() as db, pytest.raises(HTTPException) as caught:
        store_uploaded_mp3(db, upload=_upload(b"not an mp3 at all"), label="Junk")
    assert caught.value.status_code == 400
    assert _mp3_files_on_disk() == before, "the rejected file was left on disk"


def test_an_empty_upload_is_refused():
    with session_scope() as db, pytest.raises(HTTPException) as caught:
        store_uploaded_mp3(db, upload=_upload(b""), label="Nothing")
    assert caught.value.status_code == 400


def test_a_duplicate_label_is_refused():
    with session_scope() as db:
        store_uploaded_mp3(db, upload=_upload(SILENT_MP3), label="Taken")
    with session_scope() as db, pytest.raises(HTTPException) as caught:
        store_uploaded_mp3(db, upload=_upload(SILENT_MP3), label="Taken")
    assert caught.value.status_code == 400


def test_a_blank_label_is_refused():
    with session_scope() as db, pytest.raises(HTTPException) as caught:
        store_uploaded_mp3(db, upload=_upload(SILENT_MP3), label="   ")
    assert caught.value.status_code == 400


def test_only_a_chime_may_be_the_default():
    with session_scope() as db, pytest.raises(HTTPException) as caught:
        store_uploaded_mp3(db, upload=_upload(SILENT_MP3), label="Spoken",
                           kind=KIND_AUTO_RESPONSE, is_default=True)
    assert caught.value.status_code == 400


def test_a_new_default_demotes_the_old_one():
    with session_scope() as db:
        store_uploaded_mp3(db, upload=_upload(SILENT_MP3), label="First", is_default=True)
    with session_scope() as db:
        store_uploaded_mp3(db, upload=_upload(SILENT_MP3), label="Second", is_default=True)
    with session_scope() as db:
        defaults = [m.label for m in db.query(Mp3File).filter(Mp3File.is_default.is_(True))]
    assert defaults == ["Second"]
