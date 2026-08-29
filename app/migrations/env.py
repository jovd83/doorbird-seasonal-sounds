"""Alembic environment, wired to the app's own engine and models.

The database URL is deliberately taken from `app.config` rather than
`alembic.ini`, so `alembic upgrade head` always targets the same file the
running app does — including inside the container, where DATA_DIR decides it.
"""
from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db import Base, engine

# Importing the models is what populates Base.metadata for autogenerate.
from app import models  # noqa: F401  isort:skip

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it. `alembic upgrade head --sql`."""
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things in place, so every destructive change
        # goes through a table copy. Alembic calls that "batch" mode.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = context.config.attributes.get("connection", None)

    if connectable is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
            url=str(engine.url),
        )

    if hasattr(connectable, "cursor"):  # already a raw connection
        context.configure(connection=connectable, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
