"""Migrations, against all three states a database can arrive in.

This is the one change in the refactor that could destroy somebody's data: a
pre-Alembic database holds their door stations, with encrypted passwords, and
their whole audit history. Re-running the baseline over it, or leaving it
permanently unmanaged, would both be silent failures. So each state is set up
from scratch here and checked.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from app.schema import BASELINE_REVISION

# Each case runs in its own interpreter: `app.config` and the SQLAlchemy engine
# are bound at import time, so a fresh DATA_DIR needs a fresh process.
_PRELUDE = """
import os, sys
os.environ["DATA_DIR"] = {data_dir!r}
os.environ["ADMIN_PASSWORD"] = "test"
os.environ["SECRET_KEY"] = "test"
os.environ["FERNET_KEY"] = {fernet!r}
sys.path.insert(0, {repo!r})
import logging; logging.disable(logging.CRITICAL)
"""


def _run(body: str, data_dir: Path) -> str:
    from cryptography.fernet import Fernet

    script = _PRELUDE.format(
        data_dir=str(data_dir),
        fernet=Fernet.generate_key().decode(),
        repo=str(Path(__file__).resolve().parents[1]),
    ) + textwrap.dedent(body)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=180, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"subprocess failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip()


@pytest.fixture
def data_dir():
    d = Path(tempfile.mkdtemp(prefix="doorbird-schema-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_an_empty_database_is_created_and_stamped(data_dir):
    out = _run("""
        from app.db import init_db, engine
        from app.schema import current_revision
        from sqlalchemy import inspect
        init_db()
        tables = sorted(inspect(engine).get_table_names())
        print(current_revision(engine))
        print(",".join(t for t in tables if t != "alembic_version"))
    """, data_dir)
    revision, tables = out.splitlines()
    assert revision, "a fresh database was left unstamped"
    assert "devices" in tables and "schedules" in tables and "audit_log" in tables


def test_init_db_is_idempotent(data_dir):
    out = _run("""
        from app.db import init_db, engine
        from app.schema import current_revision
        init_db(); first = current_revision(engine)
        init_db(); init_db()
        print(first == current_revision(engine))
    """, data_dir)
    assert out == "True"


def test_a_pre_alembic_database_is_bridged_and_keeps_its_data(data_dir):
    """The case that matters: real rows, no alembic_version."""
    out = _run("""
        import sqlite3, os
        from pathlib import Path

        # Build a database the way a pre-Alembic install would look: the old
        # column set, no alembic_version, and a device row in it.
        os.makedirs(os.environ["DATA_DIR"], exist_ok=True)
        db = Path(os.environ["DATA_DIR"]) / "doorbird.db"
        con = sqlite3.connect(db)
        con.executescript('''
            CREATE TABLE devices (
                id INTEGER PRIMARY KEY, name VARCHAR(120) UNIQUE,
                host VARCHAR(255), username VARCHAR(120), password_enc TEXT,
                enabled BOOLEAN, use_https BOOLEAN,
                last_applied_mp3_id INTEGER, last_applied_at DATETIME,
                last_error TEXT, created_at DATETIME);
            CREATE TABLE mp3_files (
                id INTEGER PRIMARY KEY, label VARCHAR(120) UNIQUE,
                filename VARCHAR(255), size_bytes INTEGER,
                duration_seconds FLOAT, sample_rate_hz INTEGER,
                bitrate_kbps INTEGER, is_default BOOLEAN, created_at DATETIME);
            CREATE TABLE schedules (
                id INTEGER PRIMARY KEY, name VARCHAR(120),
                mp3_id INTEGER, start_month INTEGER, start_day INTEGER,
                end_month INTEGER, end_day INTEGER, year INTEGER,
                priority INTEGER, enabled BOOLEAN, created_at DATETIME);
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY, ts DATETIME, device_id INTEGER,
                mp3_id INTEGER, schedule_id INTEGER, action VARCHAR(40),
                success BOOLEAN, message TEXT);
            INSERT INTO devices (id, name, host, username, password_enc,
                                 enabled, use_https, created_at)
                VALUES (1, 'front door', '10.0.0.5', 'operator',
                        'encrypted-blob', 1, 0, '2026-01-01 10:00:00');
            INSERT INTO mp3_files (id, label, filename, size_bytes, is_default,
                                   created_at)
                VALUES (1, 'Bells', 'bells.mp3', 1234, 1, '2026-01-01 10:00:00');
            INSERT INTO schedules (id, name, mp3_id, start_month, start_day,
                                   end_month, end_day, year, priority, enabled,
                                   created_at)
                VALUES (1, 'Christmas', 1, 12, 20, 1, 6, 2026, 200, 1,
                        '2026-01-01 10:00:00');
            INSERT INTO audit_log (ts, action, success, message)
                VALUES ('2026-01-01 10:00:00', 'chime', 1, 'historic row');
        ''')
        con.commit(); con.close()

        from app.db import init_db, engine, session_scope
        from app.schema import current_revision
        from app.models import AuditLog, Device, Mp3File, Schedule

        init_db()

        print("revision:", current_revision(engine))
        with session_scope() as s:
            d = s.get(Device, 1)
            sched = s.get(Schedule, 1)
            print("device:", d.name, d.host, d.username, d.password_enc)
            print("mp3:", s.get(Mp3File, 1).label)
            print("schedule:", sched.name, sched.start_year, sched.end_year,
                  sched.kind, sched.delay_seconds)
            print("audit_rows:", s.query(AuditLog).count())
    """, data_dir)

    lines = dict(line.split(": ", 1) for line in out.splitlines())
    assert lines["revision"], "the bridged database was left unstamped"
    # Every original row survived, credentials included.
    assert lines["device"] == "front door 10.0.0.5 operator encrypted-blob"
    assert lines["mp3"] == "Bells"
    # The old single `year` column was backfilled into start_year/end_year, and
    # the columns added since the split got their defaults.
    assert lines["schedule"] == "Christmas 2026 2026 chime 0"
    assert lines["audit_rows"] == "1"


def test_a_bridged_database_does_not_run_the_bridge_twice(data_dir):
    out = _run("""
        import sqlite3, os
        from pathlib import Path
        os.makedirs(os.environ["DATA_DIR"], exist_ok=True)
        db = Path(os.environ["DATA_DIR"]) / "doorbird.db"
        con = sqlite3.connect(db)
        con.executescript('''
            CREATE TABLE devices (
                id INTEGER PRIMARY KEY, name VARCHAR(120) UNIQUE,
                host VARCHAR(255), username VARCHAR(120), password_enc TEXT,
                enabled BOOLEAN, use_https BOOLEAN,
                last_applied_mp3_id INTEGER, last_applied_at DATETIME,
                last_error TEXT, created_at DATETIME);
            INSERT INTO devices (id, name, host, username, password_enc,
                                 enabled, use_https, created_at)
                VALUES (1, 'front door', '10.0.0.5', 'op', 'blob', 1, 0, '2026-01-01');
        ''')
        con.commit(); con.close()

        import app.db as appdb
        calls = {"n": 0}
        original = appdb._run_legacy_migrations
        def counting():
            calls["n"] += 1
            original()
        appdb._run_legacy_migrations = counting

        appdb.init_db()
        appdb.init_db()
        appdb.init_db()
        print(calls["n"])
    """, data_dir)
    assert out == "1", f"the legacy bridge ran {out} times; it must run once"


def test_the_baseline_revision_id_matches_the_file():
    """A rename in one place and not the other would break every bridge."""
    versions = Path(__file__).resolve().parents[1] / "app" / "migrations" / "versions"
    baseline = versions / "0001_baseline_schema.py"
    assert baseline.exists(), "the baseline revision file is missing"
    assert f'revision: str = {BASELINE_REVISION!r}' in baseline.read_text(encoding="utf-8")


def test_migrations_have_a_single_head():
    """Two heads mean `upgrade head` becomes ambiguous and fails at boot."""
    from alembic.script import ScriptDirectory

    from app.db import engine
    from app.schema import alembic_config

    heads = ScriptDirectory.from_config(alembic_config(engine)).get_heads()
    assert len(heads) == 1, f"expected one head, found {heads}"


def test_a_v1_database_picks_up_later_revisions(data_dir):
    """The whole point of adopting Alembic: a second revision applies cleanly.

    Simulates an install that started at the baseline and is then upgraded, so
    the ladder has to run forward without touching existing rows.
    """
    out = _run("""
        from alembic import command
        from app.db import engine
        from app.schema import alembic_config, current_revision
        from sqlalchemy import inspect, text

        cfg = alembic_config(engine)

        # Stop at the baseline, as an install from that release would be.
        command.upgrade(cfg, "0001_baseline")
        print("at:", current_revision(engine))
        cols = {c["name"] for c in inspect(engine).get_columns("devices")}
        print("verify_tls_before:", "verify_tls" in cols)

        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO devices (name, host, username, password_enc, "
                "enabled, use_https, created_at) VALUES "
                "('front', '10.0.0.5', 'op', 'blob', 1, 0, '2026-01-01')"))

        # Now upgrade the rest of the way, as a container restart would.
        from app.db import init_db
        init_db()

        print("head:", current_revision(engine))
        cols = {c["name"] for c in inspect(engine).get_columns("devices")}
        print("verify_tls_after:", "verify_tls" in cols)
        with engine.connect() as c:
            row = c.execute(text(
                "SELECT name, username, verify_tls FROM devices")).fetchone()
        print("row:", row[0], row[1], row[2])
    """, data_dir)

    lines = dict(line.split(": ", 1) for line in out.splitlines())
    assert lines["at"] == "0001_baseline"
    assert lines["verify_tls_before"] == "False"
    assert lines["verify_tls_after"] == "True", "the second revision did not apply"
    assert lines["head"] != "0001_baseline", "the database stayed on the old revision"
    # The existing row survived and got the safe default.
    assert lines["row"] == "front op 0"
