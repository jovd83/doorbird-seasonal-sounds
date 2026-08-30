"""End-to-end walk through the new pages, over real HTTP.

These are the checks that would have caught a broken template or a form field
the route does not accept — the sort of thing unit tests on the helpers miss
entirely.
"""
from __future__ import annotations

import io
import re

import pytest
from sqlalchemy import delete

from app.db import init_db, session_scope
from app.main import app
from app.models import (
    KIND_AUTO_RESPONSE,
    KIND_CHIME,
    AuditLog,
    Mp3Collection,
    Mp3File,
    Schedule,
    ScheduleHoliday,
    collection_mp3s,
    schedule_devices,
)

# Environment isolation lives in conftest.py so it is applied before any import.
from tests.conftest import FormClient as TestClient

# A structurally valid MP3: silent MPEG-1 Layer III frames at 128 kbps /
# 44.1 kHz mono, which is 417 bytes each. 40 of them is a little over a
# second -- inside every limit the validator checks.
_FRAME = b"\xff\xfb\x90\xc0" + b"\x00" * 413
SILENT_MP3 = _FRAME * 40


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        c.login()
        yield c


@pytest.fixture(autouse=True)
def _clean_slate():
    """Each test starts from an empty library so counts are predictable.

    The association tables are emptied explicitly: bulk deletes bypass the
    ORM, and SQLite reuses row ids, so a leftover membership row would
    reappear attached to the next collection that happens to get the same id.
    """
    with session_scope() as db:
        db.execute(delete(schedule_devices))
        db.execute(delete(collection_mp3s))
        db.query(ScheduleHoliday).delete()
        db.query(Schedule).delete()
        db.query(Mp3Collection).delete()
        db.query(Mp3File).delete()
    yield


def _upload(client, label: str, kind: str = KIND_CHIME, default: bool = False) -> int:
    data = {"label": label, "kind": kind}
    if default:
        data["is_default"] = "true"
    r = client.post(
        "/mp3s/upload", data=data,
        files={"file": (f"{label}.mp3", io.BytesIO(SILENT_MP3), "audio/mpeg")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with session_scope() as db:
        return db.query(Mp3File).filter(Mp3File.label == label).one().id


# --- pages render ---------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/dashboard", "/schedules", "/auto-responses", "/mp3s", "/collections", "/audit",
])
def test_every_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text


def test_dashboard_has_both_schedule_tables(client):
    body = client.get("/dashboard").text
    assert "Chime schedules" in body
    assert "Auto-response schedules" in body


def test_nav_names_the_renamed_section(client):
    """Each nav entry links to its route under its own name.

    Matched with a regex rather than exact adjacency: a nav item carries an
    icon between the anchor and its label, and pinning the markup made this
    fail on a restyle that had not changed a single label.
    """
    body = client.get("/dashboard").text
    for href, label in (
        ("/schedules", "Chime schedules"),
        ("/auto-responses", "Auto responses"),
    ):
        pattern = rf'<a class="nav-item[^>]*href="{re.escape(href)}".*?{re.escape(label)}.*?</a>'
        assert re.search(pattern, body, re.DOTALL), f"nav is missing {label} → {href}"


# --- MP3 types ------------------------------------------------------------

def test_uploaded_mp3_keeps_its_type(client):
    _upload(client, "spoken", kind=KIND_AUTO_RESPONSE)
    with session_scope() as db:
        assert db.query(Mp3File).filter(Mp3File.label == "spoken").one().kind == KIND_AUTO_RESPONSE


def test_an_auto_response_clip_cannot_be_the_default(client):
    r = client.post(
        "/mp3s/upload", data={"label": "nope", "kind": KIND_AUTO_RESPONSE, "is_default": "true"},
        files={"file": ("nope.mp3", io.BytesIO(SILENT_MP3), "audio/mpeg")},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_type_can_be_changed_while_unused(client):
    mp3_id = _upload(client, "movable")
    r = client.post(f"/mp3s/{mp3_id}/kind", data={"kind": KIND_AUTO_RESPONSE},
                    follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        assert db.get(Mp3File, mp3_id).kind == KIND_AUTO_RESPONSE


def test_type_change_is_refused_while_a_schedule_uses_it(client):
    mp3_id = _upload(client, "busy")
    client.post("/schedules/create", data={
        "name": "uses-busy", "start": "2026-12-01", "end": "2026-12-26",
        "recurring": "true", "mp3_id": str(mp3_id), "all_day": "true",
    }, follow_redirects=False)
    r = client.post(f"/mp3s/{mp3_id}/kind", data={"kind": KIND_AUTO_RESPONSE},
                    follow_redirects=False)
    assert r.status_code == 400


# --- schedules of both kinds ---------------------------------------------

def test_creating_a_chime_schedule(client):
    mp3_id = _upload(client, "bells")
    r = client.post("/schedules/create", data={
        "name": "xmas", "start": "2026-12-20", "end": "2027-01-06",
        "recurring": "true", "mp3_id": str(mp3_id), "all_day": "true", "priority": "100",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    with session_scope() as db:
        s = db.query(Schedule).filter(Schedule.name == "xmas").one()
        assert s.kind == KIND_CHIME
        assert s.delay_seconds == 0


def test_creating_an_auto_response_keeps_the_wait(client):
    mp3_id = _upload(client, "porch", kind=KIND_AUTO_RESPONSE)
    r = client.post("/auto-responses/create", data={
        "name": "parcel", "start": "2026-01-01", "end": "2026-12-31",
        "recurring": "true", "mp3_id": str(mp3_id), "all_day": "true",
        "delay_seconds": "12",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    with session_scope() as db:
        s = db.query(Schedule).filter(Schedule.name == "parcel").one()
        assert s.kind == KIND_AUTO_RESPONSE
        assert s.delay_seconds == 12


def test_a_chime_schedule_refuses_an_auto_response_clip(client):
    mp3_id = _upload(client, "spoken-only", kind=KIND_AUTO_RESPONSE)
    r = client.post("/schedules/create", data={
        "name": "wrong-kind", "start": "2026-12-01", "recurring": "true",
        "mp3_id": str(mp3_id), "all_day": "true",
    }, follow_redirects=False)
    assert r.status_code == 400


def test_each_page_lists_only_its_own_kind(client):
    chime = _upload(client, "chime-clip")
    auto = _upload(client, "auto-clip", kind=KIND_AUTO_RESPONSE)
    client.post("/schedules/create", data={
        "name": "only-chime", "start": "2026-12-01", "recurring": "true",
        "mp3_id": str(chime), "all_day": "true"}, follow_redirects=False)
    client.post("/auto-responses/create", data={
        "name": "only-auto", "start": "2026-12-01", "recurring": "true",
        "mp3_id": str(auto), "all_day": "true", "delay_seconds": "3"},
        follow_redirects=False)

    assert "only-chime" in client.get("/schedules").text
    assert "only-auto" not in client.get("/schedules").text
    assert "only-auto" in client.get("/auto-responses").text
    assert "only-chime" not in client.get("/auto-responses").text


def test_an_auto_response_route_will_not_touch_a_chime_schedule(client):
    mp3_id = _upload(client, "guarded")
    client.post("/schedules/create", data={
        "name": "guarded-sched", "start": "2026-12-01", "recurring": "true",
        "mp3_id": str(mp3_id), "all_day": "true"}, follow_redirects=False)
    with session_scope() as db:
        sid = db.query(Schedule).filter(Schedule.name == "guarded-sched").one().id
    r = client.post(f"/auto-responses/{sid}/delete", follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        assert db.get(Schedule, sid) is not None


# --- collections ----------------------------------------------------------

def _make_collection(client, name: str, labels: list[str]) -> int:
    ids = [_upload(client, label) for label in labels]
    r = client.post(
        "/collections/create",
        data={"name": name, "kind": KIND_CHIME, "mp3_ids": [str(i) for i in ids]},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with session_scope() as db:
        return db.query(Mp3Collection).filter(Mp3Collection.name == name).one().id


def test_a_collection_holds_its_members(client):
    cid = _make_collection(client, "xmas-set", ["x1", "x2", "x3"])
    with session_scope() as db:
        assert len(db.get(Mp3Collection, cid).mp3s) == 3


def test_a_collection_refuses_a_clip_of_the_wrong_type(client):
    spoken = _upload(client, "spoken-member", kind=KIND_AUTO_RESPONSE)
    r = client.post("/collections/create",
                    data={"name": "mixed", "kind": KIND_CHIME, "mp3_ids": str(spoken)},
                    follow_redirects=False)
    assert r.status_code == 400


def test_a_schedule_can_point_at_a_collection(client):
    cid = _make_collection(client, "xmas-pick", ["p1", "p2", "p3"])
    r = client.post("/schedules/create", data={
        "name": "xmas-random", "start": "2026-12-20", "end": "2027-01-06",
        "recurring": "true", "collection_id": str(cid), "mp3_id": "",
        "all_day": "true",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    with session_scope() as db:
        s = db.query(Schedule).filter(Schedule.name == "xmas-random").one()
        assert s.collection_id == cid
        # The stored single file must be one of the members, so the schedule
        # always has a concrete fallback.
        assert s.mp3_id in {m.id for m in s.collection.mp3s}
        assert s.sound_label == "xmas-pick (3 random)"


def test_an_empty_collection_cannot_be_attached(client):
    r = client.post("/collections/create", data={"name": "hollow", "kind": KIND_CHIME},
                    follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        cid = db.query(Mp3Collection).filter(Mp3Collection.name == "hollow").one().id
    r = client.post("/schedules/create", data={
        "name": "uses-hollow", "start": "2026-12-01", "recurring": "true",
        "collection_id": str(cid), "mp3_id": "", "all_day": "true",
    }, follow_redirects=False)
    assert r.status_code == 400


def test_a_collection_in_use_cannot_be_deleted(client):
    cid = _make_collection(client, "locked", ["l1", "l2"])
    client.post("/schedules/create", data={
        "name": "uses-locked", "start": "2026-12-01", "recurring": "true",
        "collection_id": str(cid), "mp3_id": "", "all_day": "true"},
        follow_redirects=False)
    r = client.post(f"/collections/{cid}/delete", follow_redirects=False)
    assert r.status_code == 400


def test_deleting_a_collection_takes_its_membership_rows_with_it(client):
    """Orphaned rows would resurface against whatever reuses the row id."""
    cid = _make_collection(client, "doomed", ["d1", "d2"])
    r = client.post(f"/collections/{cid}/delete", follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        left = db.execute(
            collection_mp3s.select().where(collection_mp3s.c.collection_id == cid)
        ).all()
    assert left == []


def test_a_schedule_can_be_moved_back_to_a_single_mp3(client):
    cid = _make_collection(client, "revertible", ["r1", "r2"])
    client.post("/schedules/create", data={
        "name": "revert", "start": "2026-12-01", "recurring": "true",
        "collection_id": str(cid), "mp3_id": "", "all_day": "true"},
        follow_redirects=False)
    with session_scope() as db:
        s = db.query(Schedule).filter(Schedule.name == "revert").one()
        sid, mp3_id = s.id, s.mp3_id
    r = client.post(f"/schedules/{sid}/update", data={
        "name": "revert", "start": "2026-12-01", "recurring": "true",
        "collection_id": "", "mp3_id": str(mp3_id), "all_day": "true"},
        follow_redirects=False)
    assert r.status_code == 303, r.text
    with session_scope() as db:
        assert db.get(Schedule, sid).collection_id is None


# --- audit log ------------------------------------------------------------

def test_audit_download_is_a_csv_attachment(client):
    with session_scope() as db:
        db.add(AuditLog(action="chime", success=True, message="hello"))
    r = client.get("/audit/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "timestamp,action,device,mp3,schedule,success,message" in r.text
    assert "hello" in r.text


def test_clearing_the_log_leaves_a_record_of_the_clear(client):
    with session_scope() as db:
        db.query(AuditLog).delete()
        db.add(AuditLog(action="chime", success=True, message="before"))

    r = client.post("/audit/clear", follow_redirects=False)
    assert r.status_code == 303

    with session_scope() as db:
        rows = db.query(AuditLog).all()
        assert len(rows) == 1
        assert rows[0].action == "audit-clear"
        assert "1 entries removed" in rows[0].message


# --- appearance -----------------------------------------------------------

def test_theme_defaults_to_dark(client):
    assert 'data-theme="dark"' in client.get("/dashboard").text


def test_theme_switch_changes_every_page(client):
    r = client.post("/settings/theme", data={"theme": "light"}, follow_redirects=False)
    assert r.status_code == 303
    try:
        for path in ("/dashboard", "/settings", "/mp3s"):
            assert 'data-theme="light"' in client.get(path).text, path
    finally:
        client.post("/settings/theme", data={"theme": "dark"}, follow_redirects=False)
    assert 'data-theme="dark"' in client.get("/dashboard").text


def test_an_unknown_theme_is_refused(client):
    r = client.post("/settings/theme", data={"theme": "neon"}, follow_redirects=False)
    assert r.status_code == 303
    assert "warn=" in r.headers["location"]
    assert 'data-theme="dark"' in client.get("/dashboard").text


def test_the_login_page_follows_the_stored_theme(client):
    client.post("/settings/theme", data={"theme": "light"}, follow_redirects=False)
    try:
        with TestClient(app) as anon:
            assert 'data-theme="light"' in anon.get("/login").text
    finally:
        client.post("/settings/theme", data={"theme": "dark"}, follow_redirects=False)


def test_no_page_loads_an_external_stylesheet(client):
    """The container may sit on a LAN with no route to the internet."""
    for path in ("/dashboard", "/devices", "/schedules", "/mp3s", "/audit", "/settings"):
        body = client.get(path).text
        assert "cdn.jsdelivr.net" not in body, path
        assert "fonts.googleapis.com" not in body, path


# ------------------------------------------------- days and holidays, end to end


def test_a_schedule_can_be_given_weekdays_and_holidays(client):
    mp3_id = _upload(client, "weekday-bell")
    r = client.post("/schedules/create", data={
        "name": "office-hours", "start": "2026-01-01", "end": "2026-12-31",
        "recurring": "true", "mp3_id": str(mp3_id), "all_day": "true",
        "day_rule": "1",
        "weekdays": ["0", "1", "2", "3", "4"],
        "holiday_keys": ["christmas", "sinterklaas"],
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    with session_scope() as db:
        s = db.query(Schedule).filter(Schedule.name == "office-hours").one()
        assert s.weekday_mask == 0b0011111
        assert s.holiday_keys == {"christmas", "sinterklaas"}
        assert s.skip_public_holidays is False
        assert s.days_label == "Mo–Fr"


def test_the_skip_toggle_survives_a_round_trip(client):
    mp3_id = _upload(client, "workday-bell")
    r = client.post("/schedules/create", data={
        "name": "workdays", "start": "2026-01-01", "end": "2026-12-31",
        "recurring": "true", "mp3_id": str(mp3_id), "all_day": "true",
        "day_rule": "1", "weekdays": ["0", "1", "2", "3", "4"],
        "skip_public_holidays": "true",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    with session_scope() as db:
        s = db.query(Schedule).filter(Schedule.name == "workdays").one()
        assert s.skip_public_holidays is True
        assert s.holiday_keys == frozenset()


def test_editing_a_schedule_can_clear_every_holiday(client):
    """delete-orphan on the association, so unticking really removes rows."""
    mp3_id = _upload(client, "clearable")
    client.post("/schedules/create", data={
        "name": "clearme", "start": "2026-01-01", "end": "2026-12-31",
        "recurring": "true", "mp3_id": str(mp3_id), "all_day": "true",
        "day_rule": "1", "weekdays": ["0"], "holiday_keys": ["christmas"],
    }, follow_redirects=False)
    with session_scope() as db:
        sched = db.query(Schedule).filter(Schedule.name == "clearme").one()
        assert sched.holiday_keys == {"christmas"}
        schedule_id = sched.id

    r = client.post(f"/schedules/{schedule_id}/update", data={
        "name": "clearme", "start": "2026-01-01", "end": "2026-12-31",
        "recurring": "true", "mp3_id": str(mp3_id), "all_day": "true",
        "day_rule": "1", "weekdays": ["0", "1"],
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    with session_scope() as db:
        s = db.get(Schedule, schedule_id)
        assert s.holiday_keys == frozenset()
        assert s.weekday_mask == 0b0000011


def test_a_schedule_with_no_day_and_no_holiday_is_refused(client):
    mp3_id = _upload(client, "impossible")
    r = client.post("/schedules/create", data={
        "name": "never", "start": "2026-01-01", "end": "2026-12-31",
        "recurring": "true", "mp3_id": str(mp3_id), "all_day": "true",
        "day_rule": "1",
    }, follow_redirects=False)
    assert r.status_code == 400
    with session_scope() as db:
        assert db.query(Schedule).filter(Schedule.name == "never").first() is None


def test_a_client_that_sends_no_day_fields_still_gets_every_day(client):
    """The pre-feature request shape must keep working unchanged."""
    mp3_id = _upload(client, "legacy-bell")
    r = client.post("/schedules/create", data={
        "name": "legacy", "start": "2026-12-20", "end": "2027-01-06",
        "recurring": "true", "mp3_id": str(mp3_id), "all_day": "true",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    with session_scope() as db:
        s = db.query(Schedule).filter(Schedule.name == "legacy").one()
        assert s.weekday_mask == 0b1111111
        assert s.days_label == "Every day"


def test_an_unknown_holiday_key_is_rejected(client):
    mp3_id = _upload(client, "bad-key")
    r = client.post("/schedules/create", data={
        "name": "bogus", "start": "2026-01-01", "recurring": "true",
        "mp3_id": str(mp3_id), "all_day": "true",
        "day_rule": "1", "weekdays": ["0"], "holiday_keys": ["thanksgiving"],
    }, follow_redirects=False)
    assert r.status_code == 400


def test_the_holidays_page_lists_the_catalogue_and_its_users(client):
    mp3_id = _upload(client, "xmas-only")
    client.post("/schedules/create", data={
        "name": "just-xmas", "start": "2026-01-01", "end": "2026-12-31",
        "recurring": "true", "mp3_id": str(mp3_id), "all_day": "true",
        "day_rule": "1", "holiday_keys": ["christmas"],
    }, follow_redirects=False)

    r = client.get("/holidays")
    assert r.status_code == 200
    body = r.text
    assert "Christmas Day" in body
    assert "Easter Monday" in body
    assert "German-speaking Community" in body
    # The schedule that uses it is named on the row.
    assert "just-xmas" in body
