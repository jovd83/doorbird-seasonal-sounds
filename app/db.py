import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import ensure_data_dirs, settings

log = logging.getLogger("doorbird.db")


class Base(DeclarativeBase):
    pass


engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={
        "check_same_thread": False,
        # How long a writer waits for a competing lock before giving up. The
        # default of 5 s is short for a NAS volume under a burst of rings.
        "timeout": 30.0,
    },
    future=True,
)


@event.listens_for(engine, "connect")
def _tune_sqlite(dbapi_connection, _record) -> None:
    """Per-connection pragmas, applied to every pooled connection.

    This database has an unusual number of writers for SQLite: the request
    threadpool, one monitor watcher per device, a chime thread per ring, an
    auto-response thread per ring, and APScheduler. Under the default
    `journal_mode=delete` a reader blocks a writer outright, which is how a
    busy front door turned into "database is locked". WAL lets them proceed
    concurrently and is durable across a container restart -- it is a property
    of the database file, so it survives even though this runs on connect.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        mode = cursor.fetchone()
        # NORMAL is the right pairing with WAL: a crash can lose the last
        # transactions but the file cannot corrupt. FULL would fsync on every
        # commit, which on a spinning NAS volume is painful for no real gain
        # on an audit log.
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        # `PRAGMA foreign_keys=ON` is deliberately *not* set here. SQLite
        # leaves FK enforcement off by default, and this schema was built
        # against that: `audit_log.device_id` and `audit_log.mp3_id` are plain
        # references with no ON DELETE action, so enforcing them would make
        # deleting any device that has ever rung fail outright. Turning it on
        # needs those columns rebuilt as ON DELETE SET NULL first -- which in
        # SQLite means a full table copy -- so it belongs with the move to
        # Alembic, not in a connect hook.
        if mode and str(mode[0]).lower() != "wal":
            # Some network filesystems refuse WAL. Worth knowing about, since
            # it is the difference between concurrent and serialised writes.
            log.warning(
                "SQLite refused WAL journalling (mode is %r). Writes will "
                "serialise; expect 'database is locked' under load.", mode[0],
            )
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    """Bring the database to the current schema, whatever state it is in.

    Three cases, and they must not be confused:

    * **Brand new** — no tables at all. Alembic creates everything from the
      baseline revision forward.
    * **Pre-Alembic** — tables exist but there is no `alembic_version`. The old
      hand-rolled ladder runs one final time to bring it up to the baseline
      shape, the baseline is then *stamped* rather than executed (the tables
      are already there), and any later revisions apply normally.
    * **Managed** — `alembic_version` is present. Straight `upgrade head`.

    Getting the middle case wrong would mean either re-creating tables over
    live data or leaving a database permanently behind, so it is detected
    explicitly rather than inferred.
    """
    from app import models  # noqa: F401  (register tables)

    # The data directories are created here rather than as an import-time side
    # effect of `app.config`, so importing the package touches nothing on disk.
    ensure_data_dirs()

    from app.schema import upgrade_to_head

    upgrade_to_head(engine, legacy_bridge=_run_legacy_migrations)


def _run_legacy_migrations() -> None:
    """The pre-Alembic ladder, kept only to bridge existing databases.

    Runs at most once per database: `app.schema` calls it when it finds tables
    but no `alembic_version`, then stamps the baseline so it never runs again.
    New installs never touch it. Do **not** add to this -- new schema changes
    are Alembic revisions.
    """
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(schedules)"))}
        if "start_year" not in cols:
            conn.execute(text("ALTER TABLE schedules ADD COLUMN start_year INTEGER"))
        if "end_year" not in cols:
            conn.execute(text("ALTER TABLE schedules ADD COLUMN end_year INTEGER"))
        if "year" in cols:
            # Backfill from the old single-year column then leave it hanging:
            # SQLite's DROP COLUMN needs 3.35+ and is rarely worth the risk.
            conn.execute(text(
                "UPDATE schedules "
                "SET start_year = COALESCE(start_year, year), "
                "    end_year   = COALESCE(end_year,   year) "
                "WHERE year IS NOT NULL"
            ))

        # Time-of-day window. NULL/NULL keeps existing schedules running all day.
        if "start_minute" not in cols:
            conn.execute(text("ALTER TABLE schedules ADD COLUMN start_minute INTEGER"))
        if "end_minute" not in cols:
            conn.execute(text("ALTER TABLE schedules ADD COLUMN end_minute INTEGER"))

        # Chime vs auto-response split. Everything that existed before the
        # split was a chime, and had no post-chime delay.
        if "kind" not in cols:
            conn.execute(text(
                "ALTER TABLE schedules ADD COLUMN kind VARCHAR(20) DEFAULT 'chime'"))
            conn.execute(text("UPDATE schedules SET kind='chime' WHERE kind IS NULL"))
        if "delay_seconds" not in cols:
            conn.execute(text(
                "ALTER TABLE schedules ADD COLUMN delay_seconds INTEGER DEFAULT 0"))
            conn.execute(text(
                "UPDATE schedules SET delay_seconds=0 WHERE delay_seconds IS NULL"))

        # Collections: a schedule may draw from a bag of sounds instead of
        # naming one file. NULL keeps every existing schedule single-sound.
        if "collection_id" not in cols:
            conn.execute(text("ALTER TABLE schedules ADD COLUMN collection_id INTEGER"))

        mp3_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(mp3_files)"))}
        if "kind" not in mp3_cols:
            conn.execute(text(
                "ALTER TABLE mp3_files ADD COLUMN kind VARCHAR(20) DEFAULT 'chime'"))
            conn.execute(text("UPDATE mp3_files SET kind='chime' WHERE kind IS NULL"))

        _migrate_timestamps_to_local(conn)


def _migrate_timestamps_to_local(conn) -> None:
    """Shift historic UTC timestamps onto the local clock, exactly once.

    Rows written before the switch to local time used `datetime.utcnow()`.
    Leaving them mixed in with local-time rows would make the audit page
    inexplicably jump by the UTC offset partway down. The conversion is
    deterministic because we know the old values were UTC, and a marker in
    `app_settings` stops it ever running twice.
    """
    from app.timezone import utc_offset_hours

    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS app_settings "
        "(key VARCHAR(64) PRIMARY KEY, value TEXT)"
    ))
    marker = conn.execute(
        text("SELECT value FROM app_settings WHERE key='timestamps_localised'")
    ).fetchone()
    if marker:
        return

    offset = utc_offset_hours()
    shift = f"{offset:+g} hours"
    targets = (("audit_log", "ts"),
               ("devices", "last_applied_at"),
               ("devices", "created_at"),
               ("mp3_files", "created_at"),
               ("schedules", "created_at"))

    converted: list[str] = []
    skipped: list[str] = []
    for table, column in targets:
        try:

            # `targets` tuple a few lines up, never from a request. The shift
            # itself is a bound parameter.
            conn.execute(text(
                f"UPDATE {table} SET {column} = datetime({column}, :shift) "  # noqa: S608
                f"WHERE {column} IS NOT NULL"
            ), {"shift": shift})
            converted.append(f"{table}.{column}")
        except OperationalError as exc:
            # Only a missing table or column is survivable. Anything else is a
            # real failure and must not be mistaken for one.
            skipped.append(f"{table}.{column}")
            log.warning(
                "timestamp migration skipped %s.%s: %s", table, column, exc.orig)

    # Record *what* was converted, not just that something was. A bare marker
    # meant a partial run could never be diagnosed or resumed: the migration
    # would never fire again, and the audit page would keep mixing UTC and
    # local rows with nothing to explain why.
    marker_value = f"{shift} converted={','.join(converted) or 'none'}"
    if skipped:
        marker_value += f" skipped={','.join(skipped)}"
    conn.execute(text(
        "INSERT INTO app_settings (key, value) VALUES ('timestamps_localised', :v)"
    ), {"v": marker_value})
    log.info("timestamp migration complete: %s", marker_value)


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
