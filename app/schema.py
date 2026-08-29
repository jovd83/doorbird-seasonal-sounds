"""Schema versioning: deciding what to do with the database we were handed.

Split out of `app.db` because the decision is subtle enough to want reading on
its own, and because it must be testable against all three starting states
without spinning up the app.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect

log = logging.getLogger("doorbird.schema")

BASELINE_REVISION = "0001_baseline"

# Any of these existing means the database predates Alembic and holds real
# data. `app_settings` alone does not count: `_migrate_timestamps_to_local`
# created it with a bare CREATE TABLE IF NOT EXISTS.
_DATA_TABLES = ("devices", "mp3_files", "schedules")


def alembic_config(engine: Engine) -> Config:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "app" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    return cfg


def current_revision(engine: Engine) -> str | None:
    """The revision this database is stamped at, or None if unmanaged."""
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def has_legacy_tables(engine: Engine) -> bool:
    """True if this database holds data but was never managed by Alembic."""
    tables = set(inspect(engine).get_table_names())
    return any(t in tables for t in _DATA_TABLES)


def upgrade_to_head(
    engine: Engine,
    *,
    legacy_bridge: Callable[[], None] | None = None,
) -> None:
    """Bring the database to the newest revision, from any starting state."""
    cfg = alembic_config(engine)
    revision = current_revision(engine)

    if revision is not None:
        log.debug("database is at revision %s; upgrading to head", revision)
        command.upgrade(cfg, "head")
        return

    if has_legacy_tables(engine):
        # Pre-Alembic database with live data in it. Bring it up to the
        # baseline shape the old way, then record that it is there -- stamping
        # rather than running the baseline, because its tables already exist
        # and creating them again would fail (or, worse, wouldn't).
        log.info("database predates Alembic; bridging it to the baseline")
        if legacy_bridge is not None:
            legacy_bridge()
        command.stamp(cfg, BASELINE_REVISION)
        command.upgrade(cfg, "head")
        log.info("database bridged and upgraded to head")
        return

    log.info("empty database; creating the schema from scratch")
    command.upgrade(cfg, "head")
