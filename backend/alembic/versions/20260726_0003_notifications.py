"""Connector deliveries, notification events, and routing rules.

Revision ID: 20260726_0003
Revises: 20260725_0002
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260726_0003"
down_revision: str | None = "20260725_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column("title", sa.String(512), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("facts_json", sa.JSON(), nullable=False),
        sa.Column("schedule_id", sa.String(36), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("vm_id", sa.String(36), nullable=True),
        sa.Column("group_id", sa.String(36), nullable=True),
        sa.Column("connection_id", sa.String(36), nullable=True),
        sa.Column("fingerprint", sa.String(200), nullable=True, unique=True),
        sa.Column("read", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("type", "severity", "schedule_id", "run_id", "vm_id", "group_id", "read", "created_at"):
        op.create_index(f"ix_notification_events_{column}", "notification_events", [column])

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("notification_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connector_id", sa.String(36), nullable=False),
        sa.Column("connector_label", sa.String(200), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("external_ref", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in ("event_id", "connector_id", "status", "next_attempt_at", "created_at"):
        op.create_index(f"ix_notification_deliveries_{column}", "notification_deliveries", [column])
    op.create_index("ix_notification_deliveries_due", "notification_deliveries", ["status", "next_attempt_at"])

    op.create_table(
        "notification_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("event_types", sa.JSON(), nullable=False),
        sa.Column("min_severity", sa.String(16), nullable=False, server_default="warning"),
        sa.Column("scope_group_id", sa.String(36), nullable=True),
        sa.Column("include_subtree", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("connector_ids", sa.JSON(), nullable=False),
        sa.Column("in_app", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("digest_mode", sa.String(16), nullable=False, server_default="immediate"),
        sa.Column("digest_hour", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("digest_timezone", sa.String(100), nullable=False, server_default="America/New_York"),
        sa.Column("quiet_hours_start", sa.String(5), nullable=False, server_default=""),
        sa.Column("quiet_hours_end", sa.String(5), nullable=False, server_default=""),
        sa.Column("quiet_hours_timezone", sa.String(100), nullable=False, server_default="America/New_York"),
        sa.Column("critical_ignores_quiet_hours", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("throttle_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_digest_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_notification_rules_enabled", "notification_rules", ["enabled"])
    op.create_index("ix_notification_rules_scope_group_id", "notification_rules", ["scope_group_id"])


def downgrade() -> None:
    op.drop_table("notification_rules")
    op.drop_table("notification_deliveries")
    op.drop_table("notification_events")
