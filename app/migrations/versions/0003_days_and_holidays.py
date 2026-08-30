"""Day-of-week and Belgian-holiday rules on a schedule.

Adds the weekday bitmask, the skip-public-holidays flag, the table of holidays
a schedule is pinned to, and the store of dates for the five holidays that move
with Easter.

The mask defaults to 127 -- every day -- so every schedule that already exists
keeps firing exactly when it did. `holiday_dates` is left empty here on
purpose: `app.holiday_store.ensure_horizon` fills it on the next boot, which
keeps a hundred years of arithmetic out of a migration and makes the horizon
roll forward on its own from then on.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0003_days_holidays'
down_revision: str | None = '0002_verify_tls'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ALL_DAYS = '127'


def _existing() -> tuple[set[str], set[str]]:
    """What is already there: table names, and the columns on `schedules`.

    Needed because this revision does not only run forwards from the baseline.
    A database that predates Alembic is brought up by
    `db._run_legacy_migrations`, which calls `Base.metadata.create_all` -- and
    that builds today's model, new tables included. The bridge then stamps the
    baseline and replays every revision on top, so this one can arrive to find
    its own work already done. Checking is how the legacy ladder itself is
    written, for exactly the same reason.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    columns = (
        {c['name'] for c in inspector.get_columns('schedules')}
        if 'schedules' in tables else set()
    )
    return tables, columns


def upgrade() -> None:
    tables, columns = _existing()

    added = [
        sa.Column('weekday_mask', sa.Integer(), server_default=ALL_DAYS, nullable=False),
        sa.Column('skip_public_holidays', sa.Boolean(), server_default='0', nullable=False),
    ]
    missing = [c for c in added if c.name not in columns]
    if missing:
        with op.batch_alter_table('schedules', schema=None) as batch_op:
            for column in missing:
                batch_op.add_column(column)

    if 'schedule_holidays' in tables and 'holiday_dates' in tables:
        return

    op.create_table(
        'schedule_holidays',
        sa.Column('schedule_id', sa.Integer(), nullable=False),
        sa.Column('holiday_key', sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(['schedule_id'], ['schedules.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('schedule_id', 'holiday_key'),
    )

    op.create_table(
        'holiday_dates',
        sa.Column('holiday_key', sa.String(length=40), nullable=False),
        sa.Column('on_date', sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint('holiday_key', 'on_date'),
    )
    op.create_index(
        op.f('ix_holiday_dates_on_date'), 'holiday_dates', ['on_date'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_holiday_dates_on_date'), table_name='holiday_dates')
    op.drop_table('holiday_dates')
    op.drop_table('schedule_holidays')
    with op.batch_alter_table('schedules', schema=None) as batch_op:
        batch_op.drop_column('skip_public_holidays')
        batch_op.drop_column('weekday_mask')
