"""Groups, VM inventory, schedule runs, and schedule retargeting.

Revision ID: 20260725_0002
Revises: 20260724_0001
Create Date: 2026-07-25
"""

from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

from app.validation import parse_vm_resource_id

revision: str = "20260725_0002"
down_revision: str | None = "20260724_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamp() -> datetime:
    return datetime.now(timezone.utc)


def _columns(bind, table: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    if is_sqlite:
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")

    if "default_timezone" not in _columns(bind, "security_policy"):
        op.add_column("security_policy", sa.Column("default_timezone", sa.String(100), nullable=False, server_default="America/New_York"))

    op.create_table(
        "groups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("path", sa.Text(), nullable=False, server_default=""),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("azure_connection_id", sa.String(36), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_groups_parent_id", "groups", ["parent_id"])
    op.create_index("ix_groups_path", "groups", ["path"])
    op.create_index("ix_groups_depth", "groups", ["depth"])
    op.create_index("ix_groups_enabled", "groups", ["enabled"])
    op.create_index("ix_groups_parent_sequence", "groups", ["parent_id", "sequence"])

    op.create_table(
        "virtual_machines",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("group_id", sa.String(36), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("vm_resource_id", sa.Text(), nullable=False),
        sa.Column("normalized_resource_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("subscription_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("resource_group", sa.String(200), nullable=False, server_default=""),
        sa.Column("vm_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("azure_connection_id", sa.String(36), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_virtual_machines_group_id", "virtual_machines", ["group_id"])
    op.create_index("ix_virtual_machines_normalized_resource_id", "virtual_machines", ["normalized_resource_id"], unique=True)
    op.create_index("ix_virtual_machines_subscription_id", "virtual_machines", ["subscription_id"])
    op.create_index("ix_virtual_machines_enabled", "virtual_machines", ["enabled"])

    schedule_targets = _backfill_inventory(bind)
    if is_sqlite:
        _rebuild_schedules(bind, schedule_targets)
    else:
        _upgrade_schedules_postgresql(bind, schedule_targets)

    op.create_table(
        "schedule_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("schedule_id", sa.String(36), sa.ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True),
        sa.Column("schedule_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("mode", sa.String(16), nullable=False, server_default="mock"),
        sa.Column("trigger", sa.String(16), nullable=False, server_default="scheduler"),
        sa.Column("triggered_by", sa.String(36), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_schedule_runs_schedule_id", "schedule_runs", ["schedule_id"])
    op.create_index("ix_schedule_runs_status", "schedule_runs", ["status"])
    op.create_index("ix_schedule_runs_trigger", "schedule_runs", ["trigger"])
    op.create_index("ix_schedule_runs_scheduled_for", "schedule_runs", ["scheduled_for"])
    op.create_index("ix_schedule_runs_created_at", "schedule_runs", ["created_at"])

    if is_sqlite:
        _rebuild_start_attempts(bind, schedule_targets)
    else:
        _upgrade_start_attempts_postgresql(bind, schedule_targets)

    if is_sqlite:
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def _backfill_inventory(bind) -> dict[str, tuple[str, str]]:
    """Create an 'Ungrouped' root plus a VM per legacy schedule; returns schedule_id -> (vm_id, resource_id)."""
    if "vm_resource_id" not in _columns(bind, "schedules"):
        return {}
    rows = bind.exec_driver_sql("SELECT id, vm_resource_id FROM schedules WHERE vm_resource_id IS NOT NULL AND trim(vm_resource_id) <> ''").fetchall()
    if not rows:
        return {}
    now = _stamp()
    root_id = str(uuid4())
    bind.execute(sa.text(
        "INSERT INTO groups (id, parent_id, name, description, path, depth, sequence, azure_connection_id, enabled, created_by, created_at, updated_at)"
        " VALUES (:root_id, NULL, 'Ungrouped', 'Created automatically when schedules were migrated to groups.', :path, 0, 0, NULL, true, NULL, :created_at, :updated_at)"
    ), {"root_id": root_id, "path": f"/{root_id}/", "created_at": now, "updated_at": now})
    by_resource: dict[str, str] = {}
    targets: dict[str, tuple[str, str]] = {}
    for schedule_id, resource_id in rows:
        resource_id = str(resource_id).strip()
        normalized = resource_id.lower()
        vm_id = by_resource.get(normalized)
        if not vm_id:
            try:
                parsed = parse_vm_resource_id(resource_id)
                subscription, resource_group, vm_name = parsed.subscription_id, parsed.resource_group, parsed.vm_name
            except ValueError:
                subscription, resource_group, vm_name = "", "", resource_id.rsplit("/", 1)[-1][:200]
            vm_id = str(uuid4())
            bind.execute(sa.text(
                "INSERT INTO virtual_machines (id, group_id, vm_resource_id, normalized_resource_id, display_name, subscription_id, resource_group, vm_name,"
                " azure_connection_id, enabled, notes, created_by, created_at, updated_at)"
                " VALUES (:vm_id, :root_id, :resource_id, :normalized, :display_name, :subscription, :resource_group, :vm_name, NULL, true, '', NULL, :created_at, :updated_at)"
            ), {
                "vm_id": vm_id, "root_id": root_id, "resource_id": resource_id,
                "normalized": normalized, "display_name": vm_name, "subscription": subscription,
                "resource_group": resource_group, "vm_name": vm_name,
                "created_at": now, "updated_at": now,
            })
            by_resource[normalized] = vm_id
        targets[str(schedule_id)] = (vm_id, resource_id)
    return targets


def _rebuild_schedules(bind, targets: dict[str, tuple[str, str]]) -> None:
    columns = _columns(bind, "schedules")
    if "vm_resource_id" not in columns:
        for name, definition in (("target_type", "VARCHAR(16) NOT NULL DEFAULT 'vm'"), ("target_id", "VARCHAR(36) NOT NULL DEFAULT ''"), ("stagger_seconds", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                bind.exec_driver_sql(f'ALTER TABLE schedules ADD COLUMN {name} {definition}')
        return
    bind.exec_driver_sql("ALTER TABLE schedules RENAME TO schedules_legacy")
    op.create_table(
        "schedules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("schedule_type", sa.String(20), nullable=False),
        sa.Column("start_time", sa.String(64), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False, server_default="vm"),
        sa.Column("target_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("stagger_seconds", sa.Integer(), nullable=False, server_default="0"),
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
    bind.exec_driver_sql(
        "INSERT INTO schedules (id, name, schedule_type, start_time, timezone, target_type, target_id, stagger_seconds, azure_connection_id, enabled, notes,"
        " status, next_run_at, lease_owner, lease_until, created_by, created_at, updated_at)"
        " SELECT id, name, schedule_type, start_time, timezone, 'vm', '', 0, azure_connection_id, enabled, notes,"
        " status, next_run_at, lease_owner, lease_until, created_by, created_at, updated_at FROM schedules_legacy"
    )
    for schedule_id, (vm_id, _) in targets.items():
        bind.execute(sa.text("UPDATE schedules SET target_id = :vm_id WHERE id = :schedule_id"), {"vm_id": vm_id, "schedule_id": schedule_id})
    bind.exec_driver_sql("DROP TABLE schedules_legacy")
    op.create_index("ix_schedules_schedule_type", "schedules", ["schedule_type"])
    op.create_index("ix_schedules_enabled", "schedules", ["enabled"])
    op.create_index("ix_schedules_next_run_at", "schedules", ["next_run_at"])
    op.create_index("ix_schedules_lease_until", "schedules", ["lease_until"])
    op.create_index("ix_schedules_target_type", "schedules", ["target_type"])
    op.create_index("ix_schedules_target_id", "schedules", ["target_id"])
    op.create_index("ix_schedules_due", "schedules", ["enabled", "next_run_at", "lease_until"])
    op.create_index("ix_schedules_target", "schedules", ["target_type", "target_id"])


def _upgrade_schedules_postgresql(bind, targets: dict[str, tuple[str, str]]) -> None:
    """PostgreSQL can alter these columns in place; renaming breaks referencing FKs and indexes."""
    columns = _columns(bind, "schedules")
    additions = (
        ("target_type", sa.Column("target_type", sa.String(16), nullable=False, server_default="vm")),
        ("target_id", sa.Column("target_id", sa.String(36), nullable=False, server_default="")),
        ("stagger_seconds", sa.Column("stagger_seconds", sa.Integer(), nullable=False, server_default="0")),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("schedules", column)
    for schedule_id, (vm_id, _) in targets.items():
        bind.execute(sa.text(
            "UPDATE schedules SET target_id = :vm_id WHERE id = :schedule_id"
        ), {"vm_id": vm_id, "schedule_id": schedule_id})
    if "vm_resource_id" in columns:
        op.drop_column("schedules", "vm_resource_id")
    indexes = {str(item["name"]) for item in sa.inspect(bind).get_indexes("schedules")}
    for name, fields in (
        ("ix_schedules_target_type", ["target_type"]),
        ("ix_schedules_target_id", ["target_id"]),
        ("ix_schedules_target", ["target_type", "target_id"]),
    ):
        if name not in indexes:
            op.create_index(name, "schedules", fields)


def _rebuild_start_attempts(bind, targets: dict[str, tuple[str, str]]) -> None:
    if "vm_id" in _columns(bind, "start_attempts"):
        return
    bind.exec_driver_sql("ALTER TABLE start_attempts RENAME TO start_attempts_legacy")
    op.create_table(
        "start_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("schedule_id", sa.String(36), sa.ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("schedule_runs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("vm_id", sa.String(36), sa.ForeignKey("virtual_machines.id", ondelete="SET NULL"), nullable=True),
        sa.Column("vm_resource_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("connection_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    bind.exec_driver_sql(
        "INSERT INTO start_attempts (id, schedule_id, run_id, vm_id, vm_resource_id, connection_id, status, mode, message, attempt_number, sequence,"
        " correlation_id, claimed_at, started_at, completed_at)"
        " SELECT id, schedule_id, NULL, NULL, '', connection_id, status, mode, message, 1, 0,"
        " correlation_id, claimed_at, started_at, completed_at FROM start_attempts_legacy"
    )
    for schedule_id, (vm_id, resource_id) in targets.items():
        bind.execute(sa.text(
            "UPDATE start_attempts SET vm_id = :vm_id, vm_resource_id = :resource_id WHERE schedule_id = :schedule_id"
        ), {"vm_id": vm_id, "resource_id": resource_id, "schedule_id": schedule_id})
    bind.exec_driver_sql("DROP TABLE start_attempts_legacy")
    op.create_index("ix_start_attempts_schedule_id", "start_attempts", ["schedule_id"])
    op.create_index("ix_start_attempts_run_id", "start_attempts", ["run_id"])
    op.create_index("ix_start_attempts_vm_id", "start_attempts", ["vm_id"])
    op.create_index("ix_start_attempts_status", "start_attempts", ["status"])


def _upgrade_start_attempts_postgresql(bind, targets: dict[str, tuple[str, str]]) -> None:
    columns = _columns(bind, "start_attempts")
    additions = (
        ("run_id", sa.Column("run_id", sa.String(36), sa.ForeignKey("schedule_runs.id", ondelete="CASCADE"), nullable=True)),
        ("vm_id", sa.Column("vm_id", sa.String(36), sa.ForeignKey("virtual_machines.id", ondelete="SET NULL"), nullable=True)),
        ("vm_resource_id", sa.Column("vm_resource_id", sa.Text(), nullable=False, server_default="")),
        ("attempt_number", sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1")),
        ("sequence", sa.Column("sequence", sa.Integer(), nullable=False, server_default="0")),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("start_attempts", column)
    for schedule_id, (vm_id, resource_id) in targets.items():
        bind.execute(sa.text(
            "UPDATE start_attempts SET vm_id = :vm_id, vm_resource_id = :resource_id WHERE schedule_id = :schedule_id"
        ), {"vm_id": vm_id, "resource_id": resource_id, "schedule_id": schedule_id})

    schedule_fk = next((
        item for item in sa.inspect(bind).get_foreign_keys("start_attempts")
        if item.get("constrained_columns") == ["schedule_id"]
    ), None)
    if schedule_fk and schedule_fk.get("name"):
        op.drop_constraint(str(schedule_fk["name"]), "start_attempts", type_="foreignkey")
    op.alter_column("start_attempts", "schedule_id", existing_type=sa.String(36), nullable=True)
    op.create_foreign_key(
        "fk_start_attempts_schedule_id_schedules",
        "start_attempts", "schedules", ["schedule_id"], ["id"], ondelete="SET NULL",
    )

    indexes = {str(item["name"]) for item in sa.inspect(bind).get_indexes("start_attempts")}
    for name, fields in (
        ("ix_start_attempts_run_id", ["run_id"]),
        ("ix_start_attempts_vm_id", ["vm_id"]),
    ):
        if name not in indexes:
            op.create_index(name, "start_attempts", fields)


def downgrade() -> None:
    op.drop_table("start_attempts")
    op.drop_table("schedule_runs")
    op.drop_table("virtual_machines")
    op.drop_table("groups")
    op.drop_column("security_policy", "default_timezone")
