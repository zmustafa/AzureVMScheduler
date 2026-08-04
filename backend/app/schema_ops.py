"""Shared SQLite schema/data migration steps used by both create_all startup and Alembic."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from .validation import parse_vm_resource_id


COLUMN_ADDITIONS: dict[str, dict[str, str]] = {
    "users": {
        "email": "VARCHAR(320)",
        "external_oid": "VARCHAR(100)",
        "external_tenant_id": "VARCHAR(100)",
        "auth_source": "VARCHAR(20) NOT NULL DEFAULT 'local'",
        "is_break_glass": "BOOLEAN NOT NULL DEFAULT 0",
        "failed_login_count": "INTEGER NOT NULL DEFAULT 0",
        "locked_until": "DATETIME",
        "last_login_at": "DATETIME",
    },
    "login_sessions": {
        "revoked_at": "DATETIME",
        "auth_method": "VARCHAR(20) NOT NULL DEFAULT 'local'",
        "ip_address": "VARCHAR(64)",
        "user_agent": "VARCHAR(500)",
    },
    "security_policy": {
        "default_timezone": "VARCHAR(100) NOT NULL DEFAULT 'America/New_York'",
        "ip_lockout_enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "ip_lockout_attempts": "INTEGER NOT NULL DEFAULT 15",
        "ip_lockout_window_seconds": "INTEGER NOT NULL DEFAULT 300",
        "ip_lockout_seconds": "INTEGER NOT NULL DEFAULT 900",
        "allow_self_registration": "BOOLEAN NOT NULL DEFAULT 0",
        "ip_allowlist_mode": "VARCHAR(16) NOT NULL DEFAULT 'disabled'",
        "ip_allowlist_confirm_by": "DATETIME",
    },
    "schedules": {
        "target_type": "VARCHAR(16) NOT NULL DEFAULT 'vm'",
        "target_id": "VARCHAR(36) NOT NULL DEFAULT ''",
        "stagger_seconds": "INTEGER NOT NULL DEFAULT 0",
        "action": "VARCHAR(16) NOT NULL DEFAULT 'start'",
        "stop_mode": "VARCHAR(16) NOT NULL DEFAULT 'deallocate'",
        "ring_order": "VARCHAR(16) NOT NULL DEFAULT 'sequence'",
        "cron_expression": "VARCHAR(200) NOT NULL DEFAULT ''",
        "weekday": "INTEGER",
        "start_date": "VARCHAR(10) NOT NULL DEFAULT ''",
        "end_date": "VARCHAR(10) NOT NULL DEFAULT ''",
        "run_limit": "INTEGER",
        "run_count": "INTEGER NOT NULL DEFAULT 0",
    },
    "schedule_runs": {
        "action": "VARCHAR(16) NOT NULL DEFAULT 'start'",
        "stop_mode": "VARCHAR(16) NOT NULL DEFAULT 'deallocate'",
    },
    "vm_attempts": {
        "run_id": "VARCHAR(36)",
        "vm_id": "VARCHAR(36)",
        "vm_resource_id": "TEXT NOT NULL DEFAULT ''",
        "attempt_number": "INTEGER NOT NULL DEFAULT 1",
        "sequence": "INTEGER NOT NULL DEFAULT 0",
        "action": "VARCHAR(16) NOT NULL DEFAULT 'start'",
        "stop_mode": "VARCHAR(16) NOT NULL DEFAULT 'deallocate'",
    },
    "groups": {
        "never_stop": "BOOLEAN NOT NULL DEFAULT 0",
        "is_demo": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "virtual_machines": {
        "last_power_state": "VARCHAR(32) NOT NULL DEFAULT ''",
        "last_power_state_at": "DATETIME",
        "never_stop": "BOOLEAN NOT NULL DEFAULT 0",
    },
}


LEGACY_ATTEMPTS_TABLE = "vm_attempts_legacy"


def add_missing_model_columns(connection) -> None:
    """Add columns the models declare but the database lacks, on any dialect.

    `COLUMN_ADDITIONS` above is hand-written SQLite DDL and stays that way for the legacy local
    databases it was written for. For PostgreSQL the DDL is derived from the model metadata
    instead, so a column added in a future release lands on upgrade without anyone maintaining a
    second list — and without SQLite-only spellings such as DATETIME or BOOLEAN DEFAULT 0.

    A column is only added when it can be filled safely: nullable, or carrying a server default,
    or carrying a simple scalar Python default. Anything else is left alone for a real migration.
    """
    from sqlalchemy import inspect
    from sqlalchemy.schema import CreateColumn

    from .database import Base

    inspector = inspect(connection)
    dialect = connection.dialect
    present = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in present:
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            clause = _fill_clause(column)
            if clause is None:
                continue
            ddl = CreateColumn(column).compile(dialect=dialect).string
            # The compiled fragment carries NOT NULL but never a value for existing rows, so the
            # default is appended explicitly.
            connection.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}{clause}')


def _fill_clause(column) -> str | None:
    """The DEFAULT clause needed to add `column` to a populated table, or None if unsafe."""
    if column.server_default is not None:
        return ""  # already carried by the compiled column definition
    if column.nullable:
        return ""
    default = getattr(column.default, "arg", None)
    if default is None or callable(default):
        return None
    if isinstance(default, bool):
        return f" DEFAULT {'true' if default else 'false'}"
    if isinstance(default, (int, float)):
        return f" DEFAULT {default}"
    if isinstance(default, str):
        escaped = default.replace("'", "''")
        return f" DEFAULT '{escaped}'"
    return None


def rename_start_attempts(connection) -> None:
    """start_attempts became vm_attempts when stop waves were added; keep the existing history."""
    tables = table_names(connection)
    if "start_attempts" in tables and "vm_attempts" not in tables:
        connection.exec_driver_sql('ALTER TABLE "start_attempts" RENAME TO "vm_attempts"')


def _schedule_id_is_required(connection, table: str) -> bool:
    for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall():
        if row[1] == "schedule_id":
            return bool(row[3])
    return False


def set_aside_legacy_attempts(connection) -> None:
    """Ad-hoc waves have no schedule, but the original table made schedule_id NOT NULL.

    SQLite cannot relax a column constraint, so move the table aside and let create_all rebuild
    it from the model; copy_legacy_attempts() then restores the rows.
    """
    tables = table_names(connection)
    if "vm_attempts" not in tables or LEGACY_ATTEMPTS_TABLE in tables:
        return
    if not _schedule_id_is_required(connection, "vm_attempts"):
        return
    # Indexes follow the rename and would collide with the ones create_all is about to build.
    indexes = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='vm_attempts' AND sql IS NOT NULL"
    ).fetchall()
    for (index_name,) in indexes:
        connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{index_name}"')
    connection.exec_driver_sql(f'ALTER TABLE "vm_attempts" RENAME TO "{LEGACY_ATTEMPTS_TABLE}"')


def copy_legacy_attempts(connection) -> None:
    """Restore attempt history into the rebuilt vm_attempts table."""
    tables = table_names(connection)
    if LEGACY_ATTEMPTS_TABLE not in tables or "vm_attempts" not in tables:
        return
    shared = sorted(column_names(connection, LEGACY_ATTEMPTS_TABLE) & column_names(connection, "vm_attempts"))
    if shared:
        columns = ", ".join(f'"{name}"' for name in shared)
        connection.exec_driver_sql(
            f'INSERT INTO "vm_attempts" ({columns}) SELECT {columns} FROM "{LEGACY_ATTEMPTS_TABLE}"'
        )
    connection.exec_driver_sql(f'DROP TABLE "{LEGACY_ATTEMPTS_TABLE}"')


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def table_names(connection) -> set[str]:
    rows = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def column_names(connection, table: str) -> set[str]:
    rows = connection.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()
    return {row[1] for row in rows}


def add_missing_columns(connection) -> None:
    tables = table_names(connection)
    for table, columns in COLUMN_ADDITIONS.items():
        if table not in tables:
            continue
        existing = column_names(connection, table)
        for name, definition in columns.items():
            if name not in existing:
                connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')


def _ensure_root_group(connection, name: str = "Ungrouped") -> str:
    row = connection.exec_driver_sql(
        "SELECT id FROM groups WHERE parent_id IS NULL AND lower(name) = ?", (name.lower(),)
    ).fetchone()
    if row:
        return row[0]
    group_id, now = str(uuid4()), _stamp()
    connection.exec_driver_sql(
        "INSERT INTO groups (id, parent_id, name, description, path, depth, sequence, azure_connection_id, enabled, created_by, created_at, updated_at)"
        " VALUES (?, NULL, ?, ?, ?, 0, 0, NULL, 1, NULL, ?, ?)",
        (group_id, name, "Created automatically when schedules were migrated to groups.", f"/{group_id}/", now, now),
    )
    return group_id


def _ensure_vm(connection, group_id: str, resource_id: str) -> str:
    normalized = resource_id.strip().lower()
    row = connection.exec_driver_sql("SELECT id FROM virtual_machines WHERE normalized_resource_id = ?", (normalized,)).fetchone()
    if row:
        return row[0]
    try:
        parsed = parse_vm_resource_id(resource_id)
        subscription, resource_group, vm_name = parsed.subscription_id, parsed.resource_group, parsed.vm_name
    except ValueError:
        subscription, resource_group, vm_name = "", "", resource_id.rsplit("/", 1)[-1][:200]
    vm_id, now = str(uuid4()), _stamp()
    connection.exec_driver_sql(
        "INSERT INTO virtual_machines (id, group_id, vm_resource_id, normalized_resource_id, display_name, subscription_id, resource_group, vm_name,"
        " azure_connection_id, enabled, notes, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, '', NULL, ?, ?)",
        (vm_id, group_id, resource_id.strip(), normalized, vm_name, subscription, resource_group, vm_name, now, now),
    )
    return vm_id


def backfill_hierarchy(connection) -> None:
    """Move one-VM-per-schedule rows into the group/VM model, then retire the legacy column."""
    tables = table_names(connection)
    if not {"schedules", "groups", "virtual_machines"} <= tables:
        return
    if "vm_resource_id" not in column_names(connection, "schedules"):
        return
    rows = connection.exec_driver_sql("SELECT id, vm_resource_id FROM schedules WHERE vm_resource_id IS NOT NULL AND trim(vm_resource_id) <> ''").fetchall()
    if rows:
        root_id = _ensure_root_group(connection)
        for schedule_id, resource_id in rows:
            vm_id = _ensure_vm(connection, root_id, str(resource_id))
            connection.exec_driver_sql("UPDATE schedules SET target_type = 'vm', target_id = ? WHERE id = ?", (vm_id, schedule_id))
            if "vm_id" in column_names(connection, "start_attempts"):
                connection.exec_driver_sql(
                    "UPDATE start_attempts SET vm_id = ?, vm_resource_id = ? WHERE schedule_id = ? AND (vm_id IS NULL OR vm_id = '')",
                    (vm_id, str(resource_id).strip(), schedule_id),
                )
    connection.exec_driver_sql("ALTER TABLE schedules DROP COLUMN vm_resource_id")
