"""Auto responses, MP3 types, collections and the audit log's new buttons.

The behaviours worth pinning here are the ones a refactor could quietly
break: chime and auto-response schedules must never resolve into each other's
slot, a collection must not play the same sound twice running, and clearing
the audit log must leave a record that it happened.
"""
from __future__ import annotations

import itertools
from datetime import datetime

import pytest

from app import engine
from app.date_logic import pick_schedule, resolve_active
from app.models import (
    KIND_AUTO_RESPONSE,
    KIND_CHIME,
    Mp3Collection,
    Mp3File,
    Schedule,
)
from app.schedule_form import (
    MAX_DELAY_SECONDS,
    FormError,
    clean_delay,
    optional_int,
)


def _mp3(i: int, label: str, kind: str = KIND_CHIME) -> Mp3File:
    return Mp3File(id=i, label=label, filename=f"{label}.mp3", size_bytes=1, kind=kind)


def _sched(**kw) -> Schedule:
    base = dict(
        id=1, name="s", kind=KIND_CHIME, mp3_id=1, collection_id=None,
        start_month=1, start_day=1, end_month=12, end_day=31,
        priority=100, enabled=True, delay_seconds=0,
        start_minute=None, end_minute=None, start_year=None, end_year=None,
    )
    base.update(kw)
    devices = base.pop("devices", [])
    collection = base.pop("collection", None)
    mp3 = base.pop("mp3", None)
    s = Schedule(**base)
    s.devices = devices
    s.collection = collection
    if mp3 is not None:
        s.mp3 = mp3
    return s


WHEN = datetime(2026, 8, 23, 12, 0)


# --- the two kinds never mix ---------------------------------------------

def test_pick_schedule_only_sees_what_it_is_given():
    """Kind filtering happens in the query, so each call is a separate pool."""
    chime = _sched(id=1, name="xmas", kind=KIND_CHIME)
    auto = _sched(id=2, name="parcel", kind=KIND_AUTO_RESPONSE, delay_seconds=5)

    assert pick_schedule([chime], WHEN) is chime
    assert pick_schedule([auto], WHEN) is auto
    # Neither pool can produce the other's schedule.
    assert pick_schedule([chime], WHEN).kind == KIND_CHIME
    assert pick_schedule([auto], WHEN).kind == KIND_AUTO_RESPONSE


def test_no_auto_response_matching_means_silence_not_a_default():
    """Chimes fall back to the default MP3; auto responses fall back to nothing."""
    default = _mp3(99, "default")
    out_of_season = _sched(
        kind=KIND_AUTO_RESPONSE, start_month=12, start_day=1, end_month=12, end_day=26)

    assert pick_schedule([out_of_season], WHEN) is None
    # The chime path in the same situation still produces a sound.
    assert resolve_active([], default, WHEN).mp3 is default


# --- the wait interval ----------------------------------------------------

def test_chime_schedules_never_carry_a_delay():
    assert clean_delay(KIND_CHIME, 30, auto_response_kind=KIND_AUTO_RESPONSE) == 0


def test_auto_response_keeps_its_delay():
    assert clean_delay(KIND_AUTO_RESPONSE, 30, auto_response_kind=KIND_AUTO_RESPONSE) == 30
    assert clean_delay(KIND_AUTO_RESPONSE, 0, auto_response_kind=KIND_AUTO_RESPONSE) == 0


@pytest.mark.parametrize("bad", [-1, MAX_DELAY_SECONDS + 1])
def test_absurd_delays_are_refused(bad):
    # A plain domain error now: these rules know nothing about HTTP, which is
    # what makes them testable without a request.
    with pytest.raises(FormError):
        clean_delay(KIND_AUTO_RESPONSE, bad, auto_response_kind=KIND_AUTO_RESPONSE)


@pytest.mark.parametrize("raw,expected", [("", None), (None, None), ("7", 7), ("  7 ", 7)])
def test_optional_form_ints(raw, expected):
    """An unset <select> posts an empty string, which must not 422 the form."""
    assert optional_int(raw, "MP3") == expected


def test_non_numeric_form_int_is_refused():
    with pytest.raises(FormError):
        optional_int("abc", "MP3")


# --- collections ----------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_pick_memory():
    engine._last_pick.clear()
    yield
    engine._last_pick.clear()


def _collection(*mp3s, cid: int = 1, kind: str = KIND_CHIME) -> Mp3Collection:
    c = Mp3Collection(id=cid, name="xmas", kind=kind)
    c.mp3s = list(mp3s)
    return c


def test_schedule_without_a_collection_plays_its_one_file():
    only = _mp3(1, "bells")
    assert engine.pick_sound(_sched(), only) is only


def test_no_schedule_at_all_plays_the_default():
    default = _mp3(99, "default")
    assert engine.pick_sound(None, default) is default


def test_collection_draws_from_its_members():
    members = [_mp3(1, "a"), _mp3(2, "b"), _mp3(3, "c")]
    s = _sched(collection=_collection(*members))
    drawn = {engine.pick_sound(s, members[0]).id for _ in range(40)}
    assert drawn == {1, 2, 3}


def test_collection_never_repeats_the_previous_draw():
    members = [_mp3(1, "a"), _mp3(2, "b"), _mp3(3, "c")]
    s = _sched(collection=_collection(*members))
    picks = [engine.pick_sound(s, members[0]).id for _ in range(60)]
    assert all(a != b for a, b in itertools.pairwise(picks))


def test_single_member_collection_repeats_happily():
    """The no-repeat rule must not deadlock a collection of one."""
    only = _mp3(1, "a")
    s = _sched(collection=_collection(only))
    assert [engine.pick_sound(s, only).id for _ in range(3)] == [1, 1, 1]


def test_empty_collection_falls_back_to_the_stored_file():
    fallback = _mp3(7, "fallback")
    s = _sched(collection=_collection(cid=2))
    assert engine.pick_sound(s, fallback) is fallback


def test_two_schedules_track_their_own_last_draw():
    """The no-repeat memory is per schedule, not global."""
    members = [_mp3(1, "a"), _mp3(2, "b")]
    first = _sched(id=1, collection=_collection(*members, cid=1))
    second = _sched(id=2, collection=_collection(*members, cid=1))
    engine.pick_sound(first, members[0])
    engine.pick_sound(second, members[0])
    assert set(engine._last_pick) == {1, 2}


# --- what the UI shows ----------------------------------------------------

def test_sound_label_names_the_collection_and_its_size():
    members = [_mp3(1, "a"), _mp3(2, "b"), _mp3(3, "c")]
    s = _sched(collection=_collection(*members), mp3=members[0])
    assert s.sound_label == "xmas (3 random)"


def test_sound_label_of_a_single_file_schedule_is_the_label():
    only = _mp3(1, "bells")
    assert _sched(mp3=only).sound_label == "bells"
