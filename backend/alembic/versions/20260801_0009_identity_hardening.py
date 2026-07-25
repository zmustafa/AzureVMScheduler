"""Per-IP login throttle and the identity hardening policy knobs.

Revision ID: 20260801_0009
Revises: 20260731_0008
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260801_0009"
down_revision: str | None = "20260731_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Per-IP brute-force counter. Persisted rather than in memory so a restart cannot clear it.
    op.create_table(
        "login_throttle",
        sa.Column("ip", sa.String(64), primary_key=True),
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_login_throttle_locked_until", "login_throttle", ["locked_until"])

    op.add_column("security_policy", sa.Column("ip_lockout_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("security_policy", sa.Column("ip_lockout_attempts", sa.Integer(), nullable=False, server_default="15"))
    op.add_column("security_policy", sa.Column("ip_lockout_window_seconds", sa.Integer(), nullable=False, server_default="300"))
    op.add_column("security_policy", sa.Column("ip_lockout_seconds", sa.Integer(), nullable=False, server_default="900"))
    op.add_column("security_policy", sa.Column("allow_self_registration", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    for column in ("allow_self_registration", "ip_lockout_seconds", "ip_lockout_window_seconds", "ip_lockout_attempts", "ip_lockout_enabled"):
        op.drop_column("security_policy", column)
    op.drop_index("ix_login_throttle_locked_until", table_name="login_throttle")
    op.drop_table("login_throttle")
