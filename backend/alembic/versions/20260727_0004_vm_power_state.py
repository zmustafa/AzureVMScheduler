"""Remember the last power state read for each virtual machine.

Revision ID: 20260727_0004
Revises: 20260726_0003
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260727_0004"
down_revision: str | None = "20260726_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Power state is live data from Azure; caching the last reading lets the dashboard
    # summarise the estate without hitting ARM on every page load.
    op.add_column("virtual_machines", sa.Column("last_power_state", sa.String(32), nullable=False, server_default=""))
    op.add_column("virtual_machines", sa.Column("last_power_state_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("virtual_machines", "last_power_state_at")
    op.drop_column("virtual_machines", "last_power_state")
