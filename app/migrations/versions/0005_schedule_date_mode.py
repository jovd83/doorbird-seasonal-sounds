"""Three exclusive date rules on a schedule.

Adds `schedules.date_mode`. The calendar window used to be mandatory and the
ticked holidays merely added days *inside* it, which meant "only on Christmas"
could not be said without also inventing a range that happened to contain it.
The three are now mutually exclusive: `always`, `range`, or `holidays`.

Every existing schedule has a stored window, and a window is exactly what
`range` means, so all of them land there and nothing about when a doorbell
sounds changes.

That leaves the rows that had *both*. Under the old union rule a ticked
holiday inside an already-covered range with every weekday ticked added
nothing, so dropping those ticks is behaviour-preserving -- and leaving them
would be worse than useless: invisible in `range` mode, then springing back
into effect if the schedule were ever switched to `holidays`. They are
deleted, and the downgrade cannot bring them back.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0005_schedule_date_mode'
down_revision: str | None = '0004_device_last_ring'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column() -> bool:
    """Is the column already there?

    This revision does not only run forwards from the baseline. A database
    that predates Alembic is brought up by `Base.metadata.create_all`, which
    builds *today's* model -- `date_mode` included -- and the bridge then
    stamps the baseline and replays every revision on top. So this one can
    arrive to find its own column already made. `0003` guards the same way,
    for the same reason.
    """
    inspector = sa.inspect(op.get_bind())
    if 'schedules' not in set(inspector.get_table_names()):
        return False
    return 'date_mode' in {c['name'] for c in inspector.get_columns('schedules')}


def upgrade() -> None:
    if not _has_column():
        with op.batch_alter_table('schedules', schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                'date_mode', sa.String(length=20),
                server_default='range', nullable=False))

    # Belt and braces: the server_default already covers existing rows, but a
    # NULL here would silently read as `range` through `effective_date_mode`
    # and hide a real inconsistency from anyone reading the table directly.
    op.execute("UPDATE schedules SET date_mode = 'range' WHERE date_mode IS NULL")

    # The ticks that are now unreachable. Scoped to `range` because that is
    # what every row is at this point; written as a condition anyway so the
    # statement stays correct if this migration is ever replayed after others.
    op.execute(
        """
        DELETE FROM schedule_holidays
         WHERE schedule_id IN (
               SELECT id FROM schedules WHERE date_mode = 'range'
         )
        """
    )


def downgrade() -> None:
    with op.batch_alter_table('schedules', schema=None) as batch_op:
        batch_op.drop_column('date_mode')
