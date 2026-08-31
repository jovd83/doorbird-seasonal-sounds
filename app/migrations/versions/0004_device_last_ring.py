"""Persist the last ring per device.

Adds `devices.last_ring_at`. The dashboard's headline "Last ring" figure was
held on the watcher thread object, so it was wiped on every restart while the
audit log still listed the rings -- and it was only ever written on the
monitor.cgi path, so a ring arriving through the webhook never showed at all.

The column is backfilled from the audit log so an existing install does not
read "No rings yet" until the next press. `chime` rows are the best history
available, which means a *Play chime now* press is indistinguishable from a
real ring for the one-time backfill; everything written from here on comes
from `claim_ring`, which the manual button deliberately bypasses.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004_device_last_ring'
down_revision: str | None = '0003_days_holidays'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_ring_at', sa.DateTime(), nullable=True))

    op.execute(
        """
        UPDATE devices SET last_ring_at = (
            SELECT MAX(ts) FROM audit_log
             WHERE audit_log.device_id = devices.id
               AND audit_log.action = 'chime'
               AND audit_log.success = 1
        )
        """
    )


def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_column('last_ring_at')
