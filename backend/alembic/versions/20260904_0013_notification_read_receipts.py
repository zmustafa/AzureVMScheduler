"""Per-user notification read receipts.

Revision ID: 20260904_0013
Revises: 20260821_0012
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "20260904_0013"
down_revision: str | None = "20260821_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_event_reads",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["notification_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id", "user_id"),
    )
    op.create_index(
        "ix_notification_event_reads_user_event",
        "notification_event_reads",
        ["user_id", "event_id"],
    )
    # Preserve the old global marker for every account that existed at migration time.
    op.execute(sa.text(
        "INSERT INTO notification_event_reads (event_id, user_id, read_at) "
        "SELECT notification_events.id, users.id, CURRENT_TIMESTAMP "
        "FROM notification_events CROSS JOIN users WHERE notification_events.read = true"
    ))
    op.execute(sa.text("UPDATE notification_events SET read = false WHERE read = true"))


def downgrade() -> None:
    # Collapse receipts back to the old global meaning only when every account read an event.
    op.execute(sa.text(
        "UPDATE notification_events SET read = true WHERE id IN ("
        "SELECT event_id FROM notification_event_reads GROUP BY event_id "
        "HAVING COUNT(*) = (SELECT COUNT(*) FROM users))"
    ))
    op.drop_index("ix_notification_event_reads_user_event", table_name="notification_event_reads")
    op.drop_table("notification_event_reads")