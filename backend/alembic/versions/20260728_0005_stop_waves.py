"""Stop waves: action on schedules/runs/attempts, stop protection, attempt table rename.

Revision ID: 20260728_0005
Revises: 20260727_0004
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260728_0005"
down_revision: str | None = "20260727_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The table is no longer start-only.
    op.rename_table("start_attempts", "vm_attempts")

    # Everything that already exists was a start, so the backfill is the column default.
    op.add_column("schedules", sa.Column("action", sa.String(16), nullable=False, server_default="start"))
    op.add_column("schedules", sa.Column("stop_mode", sa.String(16), nullable=False, server_default="deallocate"))
    op.add_column("schedules", sa.Column("ring_order", sa.String(16), nullable=False, server_default="sequence"))
    op.create_index("ix_schedules_action", "schedules", ["action"])

    op.add_column("schedule_runs", sa.Column("action", sa.String(16), nullable=False, server_default="start"))
    op.add_column("schedule_runs", sa.Column("stop_mode", sa.String(16), nullable=False, server_default="deallocate"))
    op.create_index("ix_schedule_runs_action", "schedule_runs", ["action"])

    op.add_column("vm_attempts", sa.Column("action", sa.String(16), nullable=False, server_default="start"))
    op.add_column("vm_attempts", sa.Column("stop_mode", sa.String(16), nullable=False, server_default="deallocate"))
    op.create_index("ix_vm_attempts_action", "vm_attempts", ["action"])

    # On-demand waves have no schedule behind them.
    with op.batch_alter_table("vm_attempts") as batch:
        batch.alter_column("schedule_id", existing_type=sa.String(36), nullable=True)

    # Machines an operator never wants a stop wave to touch.
    op.add_column("virtual_machines", sa.Column("never_stop", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("groups", sa.Column("never_stop", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("groups", "never_stop")
    op.drop_column("virtual_machines", "never_stop")
    with op.batch_alter_table("vm_attempts") as batch:
        batch.alter_column("schedule_id", existing_type=sa.String(36), nullable=False)
    op.drop_index("ix_vm_attempts_action", table_name="vm_attempts")
    op.drop_column("vm_attempts", "stop_mode")
    op.drop_column("vm_attempts", "action")
    op.drop_index("ix_schedule_runs_action", table_name="schedule_runs")
    op.drop_column("schedule_runs", "stop_mode")
    op.drop_column("schedule_runs", "action")
    op.drop_index("ix_schedules_action", table_name="schedules")
    op.drop_column("schedules", "ring_order")
    op.drop_column("schedules", "stop_mode")
    op.drop_column("schedules", "action")
    op.rename_table("vm_attempts", "start_attempts")
