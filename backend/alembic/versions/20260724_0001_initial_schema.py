"""Initial Azure VM Scheduler schema.

Revision ID: 20260724_0001
Revises:
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260724_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_provider_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column("client_id", sa.String(100), nullable=False),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("auto_provision", sa.Boolean(), nullable=False),
        sa.Column("default_role", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "security_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("local_login_enabled", sa.Boolean(), nullable=False),
        sa.Column("password_min_length", sa.Integer(), nullable=False),
        sa.Column("password_require_upper", sa.Boolean(), nullable=False),
        sa.Column("password_require_lower", sa.Boolean(), nullable=False),
        sa.Column("password_require_number", sa.Boolean(), nullable=False),
        sa.Column("password_require_symbol", sa.Boolean(), nullable=False),
        sa.Column("lockout_attempts", sa.Integer(), nullable=False),
        sa.Column("lockout_minutes", sa.Integer(), nullable=False),
        sa.Column("session_idle_minutes", sa.Integer(), nullable=False),
        sa.Column("session_absolute_hours", sa.Integer(), nullable=False),
        sa.Column("schedule_missed_grace_seconds", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("external_oid", sa.String(100), nullable=True),
        sa.Column("external_tenant_id", sa.String(100), nullable=True),
        sa.Column("auth_source", sa.String(20), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("is_break_glass", sa.Boolean(), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("uq_users_external_identity", "users", ["external_tenant_id", "external_oid"], unique=True)
    op.create_table(
        "login_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("csrf_token", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_method", sa.String(20), nullable=False),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
    )
    op.create_index("ix_login_sessions_user_id", "login_sessions", ["user_id"])
    op.create_index("ix_login_sessions_expires_at", "login_sessions", ["expires_at"])
    op.create_index("ix_login_sessions_revoked_at", "login_sessions", ["revoked_at"])
    op.create_table(
        "schedules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("schedule_type", sa.String(20), nullable=False),
        sa.Column("start_time", sa.String(64), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("vm_resource_id", sa.Text(), nullable=False),
        sa.Column("azure_connection_id", sa.String(36), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(80), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_schedules_schedule_type", "schedules", ["schedule_type"])
    op.create_index("ix_schedules_enabled", "schedules", ["enabled"])
    op.create_index("ix_schedules_next_run_at", "schedules", ["next_run_at"])
    op.create_index("ix_schedules_lease_until", "schedules", ["lease_until"])
    op.create_index("ix_schedules_due", "schedules", ["enabled", "next_run_at", "lease_until"])
    op.create_table(
        "start_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("schedule_id", sa.String(36), sa.ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_start_attempts_schedule_id", "start_attempts", ["schedule_id"])
    op.create_index("ix_start_attempts_status", "start_attempts", ["status"])
    op.create_table(
        "import_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(80), nullable=False),
        sa.Column("target_id", sa.String(100), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("import_batches")
    op.drop_table("start_attempts")
    op.drop_table("schedules")
    op.drop_table("login_sessions")
    op.drop_table("users")
    op.drop_table("security_policy")
    op.drop_table("identity_provider_settings")
