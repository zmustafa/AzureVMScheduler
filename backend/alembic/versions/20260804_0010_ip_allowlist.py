"""The IP allowlist: allowed ranges, the enforcement policy and the coalesced block log.

Revision ID: 20260804_0010
Revises: 20260801_0009
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0010"
down_revision: str | None = "20260801_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ip_allow_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cidr", sa.String(64), nullable=False),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_user_id", sa.String(36), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ip_allow_rules_cidr", "ip_allow_rules", ["cidr"], unique=True)

    op.create_table(
        "ip_block_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ip", sa.String(64), nullable=False),
        sa.Column("path_class", sa.String(16), nullable=False, server_default="api"),
        sa.Column("last_path", sa.String(200), nullable=False, server_default=""),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("audit_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ip_block_events_ip", "ip_block_events", ["ip"])
    op.create_index("ix_ip_block_events_last_seen_at", "ip_block_events", ["last_seen_at"])

    # Off by default. An upgrade must never start refusing traffic on its own.
    op.add_column("security_policy", sa.Column("ip_allowlist_mode", sa.String(16), nullable=False, server_default="disabled"))
    op.add_column("security_policy", sa.Column("ip_allowlist_scope", sa.String(16), nullable=False, server_default="auth_only"))
    op.add_column("security_policy", sa.Column("ip_allowlist_confirm_by", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for column in ("ip_allowlist_confirm_by", "ip_allowlist_scope", "ip_allowlist_mode"):
        op.drop_column("security_policy", column)
    op.drop_index("ix_ip_block_events_last_seen_at", table_name="ip_block_events")
    op.drop_index("ix_ip_block_events_ip", table_name="ip_block_events")
    op.drop_table("ip_block_events")
    op.drop_index("ix_ip_allow_rules_cidr", table_name="ip_allow_rules")
    op.drop_table("ip_allow_rules")
