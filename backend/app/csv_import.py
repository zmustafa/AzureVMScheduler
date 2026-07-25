from __future__ import annotations

import csv
import io
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .connections import list_connections
from .models import Group, VirtualMachine, utcnow
from .recurrence import Recurrence, RecurrenceError, WEEKDAY_LABELS, one_time_at
from .recurrence import validate as recurrence_validate
from .validation import normalize_resource_id, parse_vm_resource_id


WEEKDAY_NUMBERS = {label.casefold(): number for number, label in enumerate(WEEKDAY_LABELS)}
WEEKDAY_NUMBERS.update({str(number): number for number in range(7)})


REQUIRED_COLUMNS = {"schedule_type", "start_time", "vm_resource_id"}
OPTIONAL_COLUMNS = {"timezone", "azure_connection", "name", "enabled", "notes", "action", "stop_mode", "ring_order", "cron_expression", "weekday", "start_date", "end_date", "run_limit"}
INVENTORY_REQUIRED_COLUMNS: set[str] = set()
INVENTORY_OPTIONAL_COLUMNS = {"application", "ring_path", "vm_resource_id", "vm_name", "display_name", "enabled", "notes", "azure_connection", "never_stop"}
INVENTORY_IDENTITY_COLUMNS = {"vm_resource_id", "vm_name"}
# Matches the Group.name column and GroupInput; SQLite would not enforce it for us.
MAX_GROUP_NAME = 200


def parse_enabled(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"", "true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError("enabled must be true/false, yes/no, or 1/0")


def parse_ring_path(value: str) -> list[str]:
    """A ring lives directly under its application, so at most one segment is accepted."""
    segments = [segment.strip() for segment in (value or "").split("/") if segment.strip() or value.strip()]
    if any(not segment for segment in segments):
        raise ValueError("ring_path cannot contain empty segments")
    if len(segments) > 1:
        raise ValueError("ring_path must be a single ring name — rings cannot contain other rings")
    if any(len(segment) > MAX_GROUP_NAME for segment in segments):
        raise ValueError(f"ring names cannot exceed {MAX_GROUP_NAME} characters")
    return segments


def _read_rows(content: bytes, required: set[str], optional: set[str]) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8 encoded") from exc
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    headers = set(fieldnames)
    if len(headers) != len(fieldnames):
        raise ValueError("CSV contains duplicate column names")
    missing = required - headers
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")
    unknown = headers - required - optional
    if unknown:
        raise ValueError(f"Unknown columns: {', '.join(sorted(unknown))}")
    return [{key: (value or "").strip() for key, value in source.items() if key} for source in reader]


async def _connection_lookup() -> tuple[dict[str, str], dict[str, Any] | None]:
    connections = await list_connections(public=True)
    enabled = [item for item in connections if not item.get("disabled")]
    lookup: dict[str, str] = {item["id"]: item["id"] for item in enabled}
    lookup.update({item["display_name"].casefold(): item["id"] for item in enabled})
    return lookup, next((item for item in enabled if item.get("is_default")), None)


def detect_format(content: bytes) -> str:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8 encoded") from exc
    headers = {(name or "").strip() for name in (csv.DictReader(io.StringIO(text)).fieldnames or [])}
    # The legacy schedule format is the only one carrying schedule_type; everything else is inventory.
    return "schedules" if "schedule_type" in headers else "inventory"


async def validate_csv(
    content: bytes,
    missed_grace_seconds: int = 300,
    db: AsyncSession | None = None,
    connection_id: str | None = None,
    default_path: list[str] | None = None,
) -> dict[str, Any]:
    """Dispatches between the v2 application/ring/VM inventory format and the legacy schedule format."""
    if detect_format(content) == "inventory":
        return await validate_inventory_csv(content, db, connection_id, default_path)
    return await validate_schedule_csv(content, missed_grace_seconds)


async def validate_schedule_csv(content: bytes, missed_grace_seconds: int = 300) -> dict[str, Any]:
    rows = _read_rows(content, REQUIRED_COLUMNS, OPTIONAL_COLUMNS)
    lookup, default = await _connection_lookup()
    results = []
    seen: set[tuple[str, ...]] = set()
    for index, row in enumerate(rows, start=2):
        errors: list[str] = []
        schedule_type = row.get("schedule_type", "")
        timezone_name = row.get("timezone") or "UTC"
        weekday_text = (row.get("weekday") or "").strip()
        weekday: int | None = None
        if weekday_text:
            weekday = WEEKDAY_NUMBERS.get(weekday_text.casefold()[:3])
            if weekday is None:
                errors.append(f"weekday must be Mon-Sun or 0-6, not {weekday_text!r}")
        run_limit_text = (row.get("run_limit") or "").strip()
        run_limit: int | None = None
        if run_limit_text:
            if run_limit_text.isdigit() and int(run_limit_text) >= 1:
                run_limit = int(run_limit_text)
            else:
                errors.append(f"run_limit must be a positive whole number, not {run_limit_text!r}")
        recurrence = Recurrence(
            schedule_type=schedule_type,
            timezone=timezone_name,
            start_time=row.get("start_time", ""),
            cron_expression=row.get("cron_expression", ""),
            weekday=weekday,
            start_date=(row.get("start_date") or "").strip(),
            end_date=(row.get("end_date") or "").strip(),
            run_limit=run_limit,
        )
        try:
            recurrence_validate(recurrence)
            if schedule_type == "one_time" and one_time_at(recurrence) < utcnow() - timedelta(seconds=missed_grace_seconds):
                errors.append("one_time start_time is in the past beyond the configured grace period")
        except RecurrenceError as exc:
            errors.append(str(exc))
        try:
            parse_vm_resource_id(row.get("vm_resource_id", ""))
        except ValueError as exc:
            errors.append(str(exc))
        connection_value = row.get("azure_connection", "")
        connection_id = default["id"] if default else None
        if connection_value:
            connection_id = lookup.get(connection_value) or lookup.get(connection_value.casefold())
            if not connection_id:
                errors.append(f"Unknown Azure connection: {connection_value}")
        elif not connection_id:
            errors.append("No enabled default Azure connection is configured")
        try:
            enabled = parse_enabled(row.get("enabled", ""))
        except ValueError as exc:
            errors.append(str(exc))
            enabled = True
        action = (row.get("action") or "start").strip().lower()
        if action not in {"start", "stop"}:
            errors.append(f"action must be start or stop, not {action!r}")
            action = "start"
        stop_mode = (row.get("stop_mode") or "deallocate").strip().lower()
        if stop_mode not in {"deallocate", "power_off"}:
            errors.append(f"stop_mode must be deallocate or power_off, not {stop_mode!r}")
            stop_mode = "deallocate"
        ring_order = (row.get("ring_order") or "sequence").strip().lower()
        if ring_order not in {"sequence", "reverse"}:
            errors.append(f"ring_order must be sequence or reverse, not {ring_order!r}")
            ring_order = "sequence"
        normalized = {
            "name": row.get("name") or f"VM schedule row {index}",
            "action": action,
            "stop_mode": stop_mode,
            "ring_order": ring_order,
            "schedule_type": schedule_type,
            "start_time": row.get("start_time", ""),
            "cron_expression": recurrence.cron_expression,
            "weekday": weekday,
            "start_date": recurrence.start_date,
            "end_date": recurrence.end_date,
            "run_limit": run_limit,
            "timezone": timezone_name,
            "vm_resource_id": row.get("vm_resource_id", ""),
            "azure_connection_id": connection_id,
            "enabled": enabled,
            "notes": row.get("notes", ""),
        }
        # A start and a stop on the same VM at the same time are different rows, not duplicates.
        duplicate_key = (action, schedule_type, normalized["start_time"], recurrence.cron_expression, normalized["vm_resource_id"].casefold(), connection_id or "")
        if duplicate_key in seen:
            errors.append("Duplicate schedule in this CSV")
        else:
            seen.add(duplicate_key)
        results.append({"row_number": index, "valid": not errors, "errors": errors, "data": normalized})
    return {"format": "schedules", "rows": results, "total": len(results), "valid": sum(item["valid"] for item in results), "invalid": sum(not item["valid"] for item in results)}


async def _existing_group_index(db: AsyncSession | None) -> dict[tuple[str, str], Group]:
    if db is None:
        return {}
    groups = (await db.scalars(select(Group))).all()
    return {(item.parent_id or "", item.name.strip().lower()): item for item in groups}


async def _existing_vm_ids(db: AsyncSession | None) -> set[str]:
    if db is None:
        return set()
    return set((await db.scalars(select(VirtualMachine.normalized_resource_id))).all())


async def _resolve_names(names: list[str], connection_id: str | None) -> tuple[dict[str, list[dict[str, Any]]], str]:
    """Batch-resolve bare VM names to resource IDs. One Resource Graph query covers the whole file."""
    if not names:
        return {}, ""
    if not connection_id:
        return {}, "no-connection"
    from .azure import resolve_vm_names
    from .connections import get_connection

    connection = await get_connection(connection_id)
    if not connection or connection.get("disabled"):
        return {}, "connection-unavailable"
    try:
        candidates, _source = await resolve_vm_names(connection, names, None)
    except Exception as exc:  # surfaced per row; the upload itself still previews
        return {}, f"lookup-failed: {type(exc).__name__}"
    matches: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        matches.setdefault(str(item.get("name", "")).casefold(), []).append(item)
    return matches, ""


async def validate_inventory_csv(
    content: bytes,
    db: AsyncSession | None = None,
    connection_id: str | None = None,
    default_path: list[str] | None = None,
) -> dict[str, Any]:
    """v2 inventory format. A row identifies its VM by full vm_resource_id or by bare vm_name resolved in Azure."""
    rows = _read_rows(content, INVENTORY_REQUIRED_COLUMNS, INVENTORY_OPTIONAL_COLUMNS)
    lookup, default = await _connection_lookup()
    existing_groups = await _existing_group_index(db)
    existing_vms = await _existing_vm_ids(db)
    pending_names = [row.get("vm_name", "") for row in rows if not row.get("vm_resource_id") and row.get("vm_name")]
    resolved, resolve_problem = await _resolve_names(sorted({item for item in pending_names if item}), connection_id)
    results: list[dict[str, Any]] = []
    seen_resources: set[str] = set()
    planned_groups: dict[tuple[str, ...], str] = {}
    resolved_count = 0
    for index, row in enumerate(rows, start=2):
        errors: list[str] = []
        application = row.get("application", "")
        try:
            rings = parse_ring_path(row.get("ring_path", ""))
        except ValueError as exc:
            errors.append(str(exc))
            rings = []
        if not application:
            if default_path:
                application, rings = default_path[0], list(default_path[1:]) + rings
            else:
                errors.append("application is required, or choose a destination application for the whole file")
        elif len(application) > MAX_GROUP_NAME:
            errors.append(f"application names cannot exceed {MAX_GROUP_NAME} characters")

        resource_id = row.get("vm_resource_id", "")
        vm_name = row.get("vm_name", "")
        was_resolved = False
        if not resource_id and vm_name:
            candidates = resolved.get(vm_name.casefold(), [])
            if resolve_problem == "no-connection":
                errors.append("Select the Azure tenant to resolve VM names against")
            elif resolve_problem == "connection-unavailable":
                errors.append("The selected Azure connection is missing or disabled")
            elif resolve_problem:
                errors.append(f"Azure name resolution failed ({resolve_problem})")
            elif not candidates:
                errors.append(f"No virtual machine named '{vm_name}' is visible to the selected tenant")
            elif len(candidates) > 1:
                where = ", ".join(f"{item.get('resource_group')} in {item.get('subscription_id')}" for item in candidates[:4])
                errors.append(f"'{vm_name}' exists in {len(candidates)} places ({where}) — add a vm_resource_id column for this row")
            else:
                resource_id = str(candidates[0].get("id", ""))
                was_resolved = True
                resolved_count += 1
        elif not resource_id:
            errors.append("Provide either vm_resource_id or vm_name")

        parsed = None
        if resource_id:
            try:
                parsed = parse_vm_resource_id(resource_id)
            except ValueError as exc:
                errors.append(str(exc))
        normalized = normalize_resource_id(resource_id) if resource_id else ""
        if normalized and normalized in seen_resources:
            errors.append("Duplicate VM in this CSV")
        elif normalized:
            seen_resources.add(normalized)
        if normalized and normalized in existing_vms:
            errors.append("This VM is already in the inventory")
        connection_value = row.get("azure_connection", "")
        # Rows resolved by name inherit the tenant they were looked up in, unless the CSV names one.
        connection_id_for_row = connection_id if was_resolved else None
        if connection_value:
            connection_id_for_row = lookup.get(connection_value) or lookup.get(connection_value.casefold())
            if not connection_id_for_row:
                errors.append(f"Unknown Azure connection: {connection_value}")
        try:
            enabled = parse_enabled(row.get("enabled", ""))
        except ValueError as exc:
            errors.append(str(exc))
            enabled = True
        try:
            never_stop = parse_enabled(row.get("never_stop", "")) if row.get("never_stop", "").strip() else False
        except ValueError as exc:
            errors.append(str(exc))
            never_stop = False
        if application and not errors:
            segments = (application, *rings)
            parent_key = ""
            for depth in range(len(segments)):
                prefix = segments[: depth + 1]
                found = existing_groups.get((parent_key, prefix[-1].strip().lower())) if not parent_key.startswith("planned:") else None
                if found:
                    parent_key = found.id
                    continue
                planned_groups.setdefault(prefix, "application" if depth == 0 else "ring")
                parent_key = f"planned:{'/'.join(prefix)}"
        results.append({
            "row_number": index,
            "valid": not errors,
            "errors": errors,
            "resolved_from_name": was_resolved,
            "data": {
                "application": application,
                "ring_path": "/".join(rings),
                "vm_resource_id": resource_id,
                "display_name": row.get("display_name") or (parsed.vm_name if parsed else vm_name),
                "enabled": enabled,
                "never_stop": never_stop,
                "notes": row.get("notes", ""),
                "azure_connection_id": connection_id_for_row,
            },
        })
    valid_rows = sum(item["valid"] for item in results)
    return {
        "format": "inventory",
        "rows": results,
        "total": len(results),
        "valid": valid_rows,
        "invalid": len(results) - valid_rows,
        "default_connection_id": default["id"] if default else None,
        "resolved_from_names": resolved_count,
        "names_needing_resolution": len(set(pending_names)),
        "groups_to_create": [{"path": " / ".join(path), "kind": kind} for path, kind in sorted(planned_groups.items())],
        "applications_to_create": sum(kind == "application" for kind in planned_groups.values()),
        "rings_to_create": sum(kind == "ring" for kind in planned_groups.values()),
        "vms_to_create": valid_rows,
    }
