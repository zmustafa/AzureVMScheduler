"""Richer recurrences: weekly and cron schedules, calendar bounds, and a run limit.

Revision ID: 20260729_0006
Revises: 20260728_0005
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260729_0006"
down_revision: str | None = "20260728_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows are all one_time or daily, so the empty defaults describe them correctly.
    op.add_column("schedules", sa.Column("cron_expression", sa.String(200), nullable=False, server_default=""))
    op.add_column("schedules", sa.Column("weekday", sa.Integer(), nullable=True))
    op.add_column("schedules", sa.Column("start_date", sa.String(10), nullable=False, server_default=""))
    op.add_column("schedules", sa.Column("end_date", sa.String(10), nullable=False, server_default=""))
    op.add_column("schedules", sa.Column("run_limit", sa.Integer(), nullable=True))
    op.add_column("schedules", sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("schedules", "run_count")
    op.drop_column("schedules", "run_limit")
    op.drop_column("schedules", "end_date")
    op.drop_column("schedules", "start_date")
    op.drop_column("schedules", "weekday")
    op.drop_column("schedules", "cron_expression")
