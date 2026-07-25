"""Flag demo applications so sample data can be removed precisely.

Revision ID: 20260731_0008
Revises: 20260730_0007
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260731_0008"
down_revision: str | None = "20260730_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only meaningful on an application (depth 0). Rings, VMs and schedules are reached through the
    # tree, so removal deletes exactly what the loader created and never a real application.
    op.add_column("groups", sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_groups_is_demo", "groups", ["is_demo"])


def downgrade() -> None:
    op.drop_index("ix_groups_is_demo", table_name="groups")
    op.drop_column("groups", "is_demo")
