"""Composite indexes for the scheduler's hot predicates.

Revision ID: 20260821_0012
Revises: 20260804_0011
Create Date: 2026-08-21

Every one of these backs a query the scheduler runs per attempt or per wave, where the single-column
indexes already present force a scan of one machine's or one run's whole history.
"""

from __future__ import annotations

from alembic import op


revision: str = "20260821_0012"
down_revision: str | None = "20260804_0011"
branch_labels = None
depends_on = None


_INDEXES = (
    ("ix_vm_attempts_run_status", "vm_attempts", ["run_id", "status"]),
    ("ix_vm_attempts_vm_action_status", "vm_attempts", ["vm_id", "action", "status"]),
    ("ix_vm_attempts_schedule_claimed", "vm_attempts", ["schedule_id", "claimed_at"]),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
