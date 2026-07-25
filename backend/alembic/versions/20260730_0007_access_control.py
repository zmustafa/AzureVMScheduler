"""Access control: roles, access groups, user assignments, and multi-provider SSO.

Revision ID: 20260730_0007
Revises: 20260729_0006
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260730_0007"
down_revision: str | None = "20260729_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.String(512), nullable=False, server_default=""),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("permissions_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_roles_name", "roles", ["name"], unique=True)

    # Named access_groups, never groups: that table is the application/ring hierarchy.
    op.create_table(
        "access_groups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("description", sa.String(512), nullable=False, server_default=""),
        sa.Column("role_ids_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_access_groups_name", "access_groups", ["name"], unique=True)

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role_id", sa.String(36), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table(
        "user_access_groups",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("access_group_id", sa.String(36), sa.ForeignKey("access_groups.id", ondelete="CASCADE"), primary_key=True),
    )

    op.create_table(
        "identity_providers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("type", sa.String(16), nullable=False, server_default="entra"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("button_label", sa.String(128), nullable=False, server_default=""),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_identity_providers_enabled", "identity_providers", ["enabled"])

    # Roles are seeded and users backfilled at startup by app.access, which owns the catalog and
    # stays correct as permissions are added. Doing it here would freeze today's list into history.


def downgrade() -> None:
    op.drop_index("ix_identity_providers_enabled", table_name="identity_providers")
    op.drop_table("identity_providers")
    op.drop_table("user_access_groups")
    op.drop_table("user_roles")
    op.drop_index("ix_access_groups_name", table_name="access_groups")
    op.drop_table("access_groups")
    op.drop_index("ix_roles_name", table_name="roles")
    op.drop_table("roles")
