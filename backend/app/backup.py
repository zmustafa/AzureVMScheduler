"""Settings export/import and estate reset.

The export is a portable, human-readable document: every cross-reference is carried by *name*
(group name paths, connection display names, VM resource ids) so a document can be re-imported into
a different database. Secret material is never written to the document — not in plaintext and not
encrypted. Each connection/connector instead carries a ``secret_fields`` list naming what has to be
re-entered after an import.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import get_identity_provider, get_security_policy
from .connections import upsert_connection
from .connectors.registry import connector_type, upsert_connector
from .hierarchy import ACTIONS, GroupTree, ensure_group_path, load_tree
from .models import AccessGroup, Group, NotificationRule, Role, Schedule, ScheduleRun, User, VirtualMachine, VmAttempt, new_id, utcnow
from .permissions import unknown_permissions
from .recurrence import Recurrence, one_time_at
from .recurrence import next_occurrence as recurrence_next
from .scheduling import resolve_default_timezone
from .validation import normalize_resource_id, parse_vm_resource_id


FORMAT = "azure-vm-scheduler.settings"
#: Documents exported before the product was renamed are still importable.
LEGACY_FORMATS = frozenset({"azureops.settings"})
VERSION = 1

# Import order matters: connections and connectors are referenced by everything below them.
SECTIONS: tuple[str, ...] = (
    "azure_connections",
    "connectors",
    "groups",
    "virtual_machines",
    "schedules",
    "notification_rules",
    "security_policy",
    "identity_provider",
    "roles",
    "access_groups",
)

#: Keys whose values must never reach an export document, whatever their source.
SECRET_KEYS = frozenset(
    {
        "client_secret",
        "client_secret_encrypted",
        "certificate_pem",
        "access_token_json",
        "smtp_password",
        "password",
        "signing_secret",
        "webhook_url",
        "api_token",
        "authorization",
    }
)

CONNECTION_SECRET_FIELDS = {
    "service_principal": ["client_secret"],
    "service_principal_cert": ["certificate_pem"],
    "az_cli_token": ["access_token_json"],
}
CONNECTION_EXPORT_KEYS = (
    "display_name",
    "auth_method",
    "tenant_id",
    "client_id",
    "default_subscription",
    "allow_vm_start",
    "allow_vm_stop",
    "read_only",
    "is_default",
    "disabled",
)
POLICY_EXPORT_KEYS = (
    "local_login_enabled",
    "password_min_length",
    "password_require_upper",
    "password_require_lower",
    "password_require_number",
    "password_require_symbol",
    "lockout_attempts",
    "lockout_minutes",
    "session_idle_minutes",
    "session_absolute_hours",
    "schedule_missed_grace_seconds",
    "default_timezone",
)
PROVIDER_EXPORT_KEYS = ("enabled", "tenant_id", "client_id", "auto_provision", "default_role")
RULE_EXPORT_KEYS = (
    "name",
    "enabled",
    "event_types",
    "min_severity",
    "include_subtree",
    "in_app",
    "digest_mode",
    "digest_hour",
    "digest_timezone",
    "quiet_hours_start",
    "quiet_hours_end",
    "quiet_hours_timezone",
    "critical_ignores_quiet_hours",
    "throttle_minutes",
)


# -- export ------------------------------------------------------------


def name_path(tree: GroupTree, group_id: str | None) -> list[str]:
    """Root-to-leaf list of group names; the portable identity of a node."""
    return [node.name for node in reversed(tree.chain(group_id))]


def connector_secret_fields(type_id: str, mode: str) -> list[str]:
    try:
        specs = connector_type(type_id).modes.get(mode, ())
    except ValueError:
        return []
    return sorted(spec.key for spec in specs if spec.secret)


def _connector_public_config(item: dict[str, Any]) -> dict[str, Any]:
    """Allow-list the non-secret spec fields; anything unrecognised is dropped rather than trusted."""
    try:
        specs = connector_type(item.get("type", "")).modes.get(item.get("mode", ""), ())
    except ValueError:
        return {}
    allowed = {spec.key for spec in specs if not spec.secret}
    stored = item.get("config") or {}
    return {key: value for key, value in stored.items() if key in allowed}


def _export_connection(item: dict[str, Any]) -> dict[str, Any]:
    auth_method = str(item.get("auth_method") or "azure_cli")
    safe = {key: item.get(key) for key in CONNECTION_EXPORT_KEYS if item.get(key) is not None}
    safe["auth_method"] = auth_method
    safe["secret_fields"] = list(CONNECTION_SECRET_FIELDS.get(auth_method, []))
    return safe


def _export_connector(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name", ""),
        "type": item.get("type", ""),
        "mode": item.get("mode", ""),
        "disabled": bool(item.get("disabled", False)),
        "config": _connector_public_config(item),
        "secret_fields": connector_secret_fields(item.get("type", ""), item.get("mode", "")),
    }


async def build_export(
    db: AsyncSession,
    connections: list[dict[str, Any]],
    connectors: list[dict[str, Any]],
    app_version: str = "",
) -> dict[str, Any]:
    """Assemble the portable settings document. `connections`/`connectors` are the *public* views."""
    tree = await load_tree(db)
    connection_names = {item["id"]: str(item.get("display_name") or "") for item in connections}
    policy = await get_security_policy(db)
    provider = await get_identity_provider(db)

    groups = sorted(tree.by_id.values(), key=lambda node: (node.depth, node.sequence, node.name.lower()))
    group_items = [
        {
            "name_path": name_path(tree, node.id),
            "description": node.description,
            "sequence": node.sequence,
            "enabled": node.enabled,
            "never_stop": node.never_stop,
            "azure_connection": connection_names.get(node.azure_connection_id or ""),
        }
        for node in groups
    ]

    vms = (await db.scalars(select(VirtualMachine).order_by(VirtualMachine.vm_name, VirtualMachine.id))).all()
    vm_paths = {item.id: item.vm_resource_id for item in vms}
    vm_items = [
        {
            "vm_resource_id": item.vm_resource_id,
            "display_name": item.display_name,
            "enabled": item.enabled,
            "never_stop": item.never_stop,
            "notes": item.notes,
            "group_path": name_path(tree, item.group_id),
            "azure_connection": connection_names.get(item.azure_connection_id or ""),
        }
        for item in vms
    ]

    schedules = (await db.scalars(select(Schedule).order_by(Schedule.name, Schedule.id))).all()
    schedule_items = []
    for item in schedules:
        target = (
            {"type": "group", "path": name_path(tree, item.target_id)}
            if item.target_type == "group"
            else {"type": "vm", "vm_resource_id": vm_paths.get(item.target_id, "")}
        )
        schedule_items.append(
            {
                "name": item.name,
                "action": item.action,
                "stop_mode": item.stop_mode,
                "ring_order": item.ring_order,
                "schedule_type": item.schedule_type,
                "start_time": item.start_time,
                "cron_expression": item.cron_expression,
                "weekday": item.weekday,
                "start_date": item.start_date,
                "end_date": item.end_date,
                "run_limit": item.run_limit,
                "timezone": item.timezone,
                "stagger_seconds": item.stagger_seconds,
                "enabled": item.enabled,
                "notes": item.notes,
                "azure_connection": connection_names.get(item.azure_connection_id or ""),
                "target": target,
            }
        )

    connector_names = {item["id"]: str(item.get("name") or "") for item in connectors}
    rules = (await db.scalars(select(NotificationRule).order_by(NotificationRule.name, NotificationRule.id))).all()
    rule_items = [
        {
            **{key: getattr(rule, key) for key in RULE_EXPORT_KEYS},
            "connectors": [connector_names[item] for item in (rule.connector_ids or []) if item in connector_names],
            "scope_group_path": name_path(tree, rule.scope_group_id) if rule.scope_group_id else None,
        }
        for rule in rules
    ]

    all_roles = (await db.scalars(select(Role).order_by(Role.name))).all()
    role_names = {role.id: role.name for role in all_roles}
    custom_roles = [role for role in all_roles if not role.is_system]
    access_groups = (await db.scalars(select(AccessGroup).order_by(AccessGroup.name))).all()

    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "app_version": app_version,
        "groups": group_items,
        "virtual_machines": vm_items,
        "schedules": schedule_items,
        "azure_connections": [_export_connection(item) for item in connections],
        "connectors": [_export_connector(item) for item in connectors],
        "notification_rules": rule_items,
        "security_policy": {key: getattr(policy, key) for key in POLICY_EXPORT_KEYS} | {"default_timezone": resolve_default_timezone(policy)},
        "identity_provider": {key: getattr(provider, key) for key in PROVIDER_EXPORT_KEYS},
        # Custom roles and access groups only. Built-in roles are owned by the application, so
        # exporting them would let an old document silently roll back a permission added since.
        "roles": [
            {"name": role.name, "description": role.description, "permissions": list(role.permissions_json or [])}
            for role in custom_roles
        ],
        "access_groups": [
            {"name": group.name, "description": group.description, "roles": [role_names.get(str(item), "") for item in (group.role_ids_json or []) if role_names.get(str(item))]}
            for group in access_groups
        ],
    }


# -- import ------------------------------------------------------------


class BackupDocumentError(ValueError):
    """Raised for documents this build cannot read."""


def validate_document(document: Any) -> None:
    if not isinstance(document, dict):
        raise BackupDocumentError("The settings document must be a JSON object")
    if document.get("format") not in {FORMAT, *LEGACY_FORMATS}:
        raise BackupDocumentError(f"Unsupported document format: expected '{FORMAT}'")
    try:
        version = int(document.get("version"))
    except (TypeError, ValueError) as exc:
        raise BackupDocumentError("The settings document is missing a numeric version") from exc
    if version != VERSION:
        raise BackupDocumentError(f"Unsupported document version {version}: this build reads version {VERSION}")


def resolve_sections(requested: list[str] | None) -> list[str]:
    if not requested:
        return list(SECTIONS)
    unknown = sorted(set(requested) - set(SECTIONS))
    if unknown:
        raise BackupDocumentError(f"Unknown section(s): {', '.join(unknown)}")
    return [item for item in SECTIONS if item in set(requested)]


class _Summary:
    def __init__(self) -> None:
        self.sections: dict[str, dict[str, Any]] = {}

    def section(self, name: str) -> dict[str, Any]:
        return self.sections.setdefault(name, {"created": 0, "skipped": 0, "failed": 0, "details": []})

    def created(self, name: str, detail: str) -> None:
        bucket = self.section(name)
        bucket["created"] += 1
        bucket["details"].append({"outcome": "created", "message": detail})

    def skipped(self, name: str, detail: str) -> None:
        bucket = self.section(name)
        bucket["skipped"] += 1
        bucket["details"].append({"outcome": "skipped", "message": detail})

    def failed(self, name: str, detail: str) -> None:
        bucket = self.section(name)
        bucket["failed"] += 1
        bucket["details"].append({"outcome": "failed", "message": detail})

    def as_dict(self, mode: str, dry_run: bool, needs_secret: list[str], removed: dict[str, int] | None) -> dict[str, Any]:
        return {
            "mode": mode,
            "dry_run": dry_run,
            "sections": self.sections,
            "created": sum(item["created"] for item in self.sections.values()),
            "skipped": sum(item["skipped"] for item in self.sections.values()),
            "failed": sum(item["failed"] for item in self.sections.values()),
            "needs_secret": needs_secret,
            "removed": removed or {},
        }


async def _find_group_by_path(db: AsyncSession, segments: list[str]) -> Group | None:
    parent: Group | None = None
    for name in segments:
        statement = select(Group).where(func.lower(Group.name) == name.strip().lower())
        statement = statement.where(Group.parent_id.is_(None)) if parent is None else statement.where(Group.parent_id == parent.id)
        found = await db.scalar(statement.limit(1))
        if not found:
            return None
        parent = found
    return parent


def _clean_path(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("name_path must be a list of group names")
    segments = [str(item).strip() for item in value if str(item).strip()]
    if not segments:
        raise ValueError("name_path cannot be empty")
    return segments


async def reset_estate(db: AsyncSession) -> dict[str, int]:
    """Remove the whole schedulable estate. Users, sessions, audit history and credentials survive."""
    counts = {
        "groups_removed": int(await db.scalar(select(func.count()).select_from(Group)) or 0),
        "vms_removed": int(await db.scalar(select(func.count()).select_from(VirtualMachine)) or 0),
        "schedules_removed": int(await db.scalar(select(func.count()).select_from(Schedule)) or 0),
        "runs_removed": int(await db.scalar(select(func.count()).select_from(ScheduleRun)) or 0),
    }
    # Explicit ordering: the test engine and some deployments do not enforce ON DELETE cascades.
    await db.execute(delete(VmAttempt))
    await db.execute(delete(ScheduleRun))
    await db.execute(delete(Schedule))
    await db.execute(delete(VirtualMachine))
    await db.execute(delete(Group))
    return counts


async def _import_connections(
    document: dict[str, Any],
    connections: list[dict[str, Any]],
    summary: _Summary,
    needs_secret: list[str],
    dry_run: bool,
) -> dict[str, str]:
    """Returns the name -> id map used by every later section."""
    by_name = {str(item.get("display_name") or "").casefold(): item["id"] for item in connections}
    had_connections = bool(connections)
    for raw in document.get("azure_connections") or []:
        name = str(raw.get("display_name") or "").strip()
        try:
            if not name:
                raise ValueError("display_name is required")
            if name.casefold() in by_name:
                summary.skipped("azure_connections", f"{name} already exists")
                continue
            required = list(raw.get("secret_fields") or CONNECTION_SECRET_FIELDS.get(str(raw.get("auth_method") or ""), []))
            payload: dict[str, Any] = {key: raw.get(key) for key in CONNECTION_EXPORT_KEYS if raw.get(key) is not None}
            payload["display_name"] = name
            payload["is_default"] = bool(raw.get("is_default")) and not had_connections and not required
            if required:
                # The document could not carry the credential, so land it inert and say so.
                payload["auth_method"] = "azure_cli"
                payload["disabled"] = True
                needs_secret.append(f"Azure connection '{name}' needs {', '.join(required)}")
            if dry_run:
                by_name[name.casefold()] = f"pending:{name}"
                summary.created("azure_connections", f"{name} would be created" + (" (disabled until secrets are re-entered)" if required else ""))
                continue
            created = await upsert_connection(payload)
            by_name[name.casefold()] = created["id"]
            summary.created("azure_connections", f"{name} created" + (" disabled; re-enter the secret and restore the auth method" if required else ""))
        except Exception as exc:
            summary.failed("azure_connections", f"{name or 'connection'}: {exc}")
    return by_name


async def _import_connectors(
    document: dict[str, Any],
    connectors: list[dict[str, Any]],
    summary: _Summary,
    needs_secret: list[str],
    dry_run: bool,
) -> dict[str, str]:
    by_name = {str(item.get("name") or "").casefold(): item["id"] for item in connectors}
    for raw in document.get("connectors") or []:
        name = str(raw.get("name") or "").strip()
        try:
            if not name:
                raise ValueError("name is required")
            if name.casefold() in by_name:
                summary.skipped("connectors", f"{name} already exists")
                continue
            required = list(raw.get("secret_fields") or connector_secret_fields(str(raw.get("type") or ""), str(raw.get("mode") or "")))
            payload = {
                "name": name,
                "type": str(raw.get("type") or ""),
                "mode": str(raw.get("mode") or ""),
                "disabled": bool(raw.get("disabled")) or bool(required),
                "config": dict(raw.get("config") or {}),
            }
            if required:
                needs_secret.append(f"Connector '{name}' needs {', '.join(required)}")
            if dry_run:
                connector_type(payload["type"])  # surfaces an unknown type during preview
                by_name[name.casefold()] = f"pending:{name}"
                summary.created("connectors", f"{name} would be created" + (" (disabled until secrets are re-entered)" if required else ""))
                continue
            created = await upsert_connector(payload, allow_incomplete=bool(required))
            by_name[name.casefold()] = created["id"]
            summary.created("connectors", f"{name} created" + (" disabled; re-enter the secret before enabling" if required else ""))
        except Exception as exc:
            summary.failed("connectors", f"{name or 'connector'}: {exc}")
    return by_name


async def _import_groups(
    db: AsyncSession,
    document: dict[str, Any],
    connection_ids: dict[str, str],
    user: User | None,
    summary: _Summary,
) -> None:
    items = sorted(document.get("groups") or [], key=lambda item: len(item.get("name_path") or []))
    for raw in items:
        label = " / ".join(str(part) for part in (raw.get("name_path") or [])) or "group"
        try:
            segments = _clean_path(raw.get("name_path"))
            if await _find_group_by_path(db, segments):
                summary.skipped("groups", f"{label} already exists")
                continue
            group = await ensure_group_path(db, segments, created_by=user.id if user else None)
            group.description = str(raw.get("description") or "")
            group.enabled = bool(raw.get("enabled", True))
            group.never_stop = bool(raw.get("never_stop", False))
            if raw.get("sequence") is not None:
                group.sequence = int(raw["sequence"])
            connection_name = str(raw.get("azure_connection") or "").casefold()
            group.azure_connection_id = connection_ids.get(connection_name) if connection_name else None
            await db.flush()
            summary.created("groups", f"{label} created")
        except Exception as exc:
            summary.failed("groups", f"{label}: {exc}")


async def _import_vms(
    db: AsyncSession,
    document: dict[str, Any],
    connection_ids: dict[str, str],
    user: User | None,
    summary: _Summary,
) -> None:
    for raw in document.get("virtual_machines") or []:
        resource_id = str(raw.get("vm_resource_id") or "").strip()
        try:
            parsed = parse_vm_resource_id(resource_id)
            normalized = normalize_resource_id(resource_id)
            if await db.scalar(select(VirtualMachine.id).where(VirtualMachine.normalized_resource_id == normalized)):
                summary.skipped("virtual_machines", f"{parsed.vm_name} is already in the inventory")
                continue
            group = await ensure_group_path(db, _clean_path(raw.get("group_path")), created_by=user.id if user else None)
            connection_name = str(raw.get("azure_connection") or "").casefold()
            db.add(
                VirtualMachine(
                    id=new_id(),
                    group_id=group.id,
                    vm_resource_id=resource_id,
                    normalized_resource_id=normalized,
                    display_name=str(raw.get("display_name") or parsed.vm_name),
                    subscription_id=parsed.subscription_id,
                    resource_group=parsed.resource_group,
                    vm_name=parsed.vm_name,
                    azure_connection_id=connection_ids.get(connection_name) if connection_name else None,
                    enabled=bool(raw.get("enabled", True)),
                    never_stop=bool(raw.get("never_stop", False)),
                    notes=str(raw.get("notes") or ""),
                    created_by=user.id if user else None,
                )
            )
            await db.flush()
            summary.created("virtual_machines", f"{parsed.vm_name} created")
        except Exception as exc:
            summary.failed("virtual_machines", f"{resource_id or 'virtual machine'}: {exc}")


async def _import_schedules(
    db: AsyncSession,
    document: dict[str, Any],
    connection_ids: dict[str, str],
    user: User | None,
    summary: _Summary,
) -> None:
    policy = await get_security_policy(db)
    default_zone = resolve_default_timezone(policy)
    grace = timedelta(seconds=policy.schedule_missed_grace_seconds)
    for raw in document.get("schedules") or []:
        name = str(raw.get("name") or "").strip() or "schedule"
        try:
            target = raw.get("target") or {}
            if str(target.get("type")) == "group":
                group = await _find_group_by_path(db, _clean_path(target.get("path")))
                if not group:
                    raise ValueError("target group does not exist")
                target_type, target_id = "group", group.id
            else:
                normalized = normalize_resource_id(str(target.get("vm_resource_id") or ""))
                vm = await db.scalar(select(VirtualMachine).where(VirtualMachine.normalized_resource_id == normalized))
                if not vm:
                    raise ValueError("target virtual machine does not exist")
                target_type, target_id = "vm", vm.id
            schedule_type = str(raw.get("schedule_type") or "")
            start_time = str(raw.get("start_time") or "")
            action = str(raw.get("action") or "start")
            if action not in ACTIONS:
                raise ValueError(f"unknown action {action!r}")
            cron_expression = str(raw.get("cron_expression") or "")
            weekday = raw.get("weekday")
            weekday = int(weekday) if weekday is not None else None
            # A start and a stop may legitimately share a target and time, so action is part of identity.
            duplicate = await db.scalar(
                select(Schedule.id).where(
                    Schedule.target_type == target_type,
                    Schedule.target_id == target_id,
                    Schedule.schedule_type == schedule_type,
                    Schedule.start_time == start_time,
                    Schedule.cron_expression == cron_expression,
                    Schedule.action == action,
                ).limit(1)
            )
            if duplicate:
                summary.skipped("schedules", f"{name} already exists for this target")
                continue
            zone = str(raw.get("timezone") or default_zone)
            recurrence = Recurrence(
                schedule_type=schedule_type,
                timezone=zone,
                start_time=start_time,
                cron_expression=cron_expression,
                weekday=weekday,
                start_date=str(raw.get("start_date") or ""),
                end_date=str(raw.get("end_date") or ""),
                run_limit=int(raw["run_limit"]) if raw.get("run_limit") is not None else None,
            )
            enabled = bool(raw.get("enabled", True))
            if schedule_type == "one_time":
                moment = one_time_at(recurrence)
                stale = moment < utcnow() - grace
                next_run = moment
            else:
                next_run = recurrence_next(recurrence)
                # A restored recurrence whose window has closed is kept but left switched off.
                stale = next_run is None
            if stale:
                enabled = False
            connection_name = str(raw.get("azure_connection") or "").casefold()
            db.add(
                Schedule(
                    id=new_id(),
                    name=name,
                    action=action,
                    stop_mode=str(raw.get("stop_mode") or "deallocate"),
                    ring_order=str(raw.get("ring_order") or "sequence"),
                    schedule_type=schedule_type,
                    start_time=start_time,
                    cron_expression=cron_expression,
                    weekday=weekday,
                    start_date=recurrence.start_date,
                    end_date=recurrence.end_date,
                    run_limit=recurrence.run_limit,
                    timezone=zone,
                    target_type=target_type,
                    target_id=target_id,
                    stagger_seconds=int(raw.get("stagger_seconds") or 0),
                    azure_connection_id=connection_ids.get(connection_name) if connection_name else None,
                    enabled=enabled,
                    notes=str(raw.get("notes") or ""),
                    status="scheduled" if enabled else "disabled",
                    next_run_at=next_run if enabled else None,
                    created_by=user.id if user else None,
                )
            )
            await db.flush()
            summary.created("schedules", f"{name} created" + (" but left disabled: it has no future occurrences" if stale else ""))
        except Exception as exc:
            summary.failed("schedules", f"{name}: {exc}")


async def _import_rules(
    db: AsyncSession,
    document: dict[str, Any],
    connector_ids: dict[str, str],
    user: User | None,
    summary: _Summary,
) -> None:
    for raw in document.get("notification_rules") or []:
        name = str(raw.get("name") or "").strip() or "rule"
        try:
            if await db.scalar(select(NotificationRule.id).where(func.lower(NotificationRule.name) == name.lower()).limit(1)):
                summary.skipped("notification_rules", f"{name} already exists")
                continue
            missing = [str(item) for item in (raw.get("connectors") or []) if str(item).casefold() not in connector_ids]
            resolved = [connector_ids[str(item).casefold()] for item in (raw.get("connectors") or []) if str(item).casefold() in connector_ids]
            scope_id = None
            scope_path = raw.get("scope_group_path")
            if scope_path:
                scope = await _find_group_by_path(db, _clean_path(scope_path))
                if not scope:
                    raise ValueError("scope group does not exist")
                scope_id = scope.id
            values = {key: raw[key] for key in RULE_EXPORT_KEYS if key in raw}
            rule = NotificationRule(id=new_id(), created_by=user.id if user else None, scope_group_id=scope_id, connector_ids=resolved)
            for key, value in values.items():
                setattr(rule, key, value)
            db.add(rule)
            await db.flush()
            suffix = f"; unresolved connector(s): {', '.join(missing)}" if missing else ""
            summary.created("notification_rules", f"{name} created{suffix}")
        except Exception as exc:
            summary.failed("notification_rules", f"{name}: {exc}")


async def _import_policy(db: AsyncSession, document: dict[str, Any], summary: _Summary) -> None:
    raw = document.get("security_policy")
    if not isinstance(raw, dict):
        summary.skipped("security_policy", "not present in the document")
        return
    from .schemas import SecurityPolicyUpdate

    try:
        parsed = SecurityPolicyUpdate.model_validate({key: raw[key] for key in POLICY_EXPORT_KEYS if key in raw})
    except Exception as exc:
        summary.failed("security_policy", str(exc))
        return
    policy = await get_security_policy(db)
    for key, value in parsed.model_dump().items():
        setattr(policy, key, value)
    await db.flush()
    summary.created("security_policy", "applied")


async def _import_provider(db: AsyncSession, document: dict[str, Any], summary: _Summary) -> None:
    raw = document.get("identity_provider")
    if not isinstance(raw, dict):
        summary.skipped("identity_provider", "not present in the document")
        return
    provider = await get_identity_provider(db)
    provider.enabled = bool(raw.get("enabled", False)) and bool(provider.client_secret_encrypted)
    provider.tenant_id = str(raw.get("tenant_id") or "")[:100]
    provider.client_id = str(raw.get("client_id") or "")[:100]
    provider.auto_provision = bool(raw.get("auto_provision", False))
    provider.default_role = str(raw.get("default_role") or "viewer")
    await db.flush()
    note = "applied" if provider.enabled or not raw.get("enabled") else "applied but left disabled until the Entra client secret is re-entered"
    summary.created("identity_provider", note)


async def _import_roles(db: AsyncSession, document: dict[str, Any], summary: _Summary) -> None:
    """Restore custom roles. Built-in roles are owned by the catalog and are never touched."""
    existing = {role.name.casefold() for role in (await db.scalars(select(Role))).all()}
    for raw in document.get("roles") or []:
        name = str(raw.get("name") or "").strip()
        try:
            if not name:
                raise ValueError("a role needs a name")
            if name.casefold() in existing:
                summary.skipped("roles", f"{name} already exists")
                continue
            permissions = [str(item) for item in (raw.get("permissions") or [])]
            unknown = unknown_permissions(permissions)
            if unknown:
                raise ValueError(f"unknown permission(s): {', '.join(unknown)}")
            db.add(Role(id=new_id(), name=name, description=str(raw.get("description") or ""), is_system=False, permissions_json=permissions))
            existing.add(name.casefold())
            await db.flush()
            summary.created("roles", f"{name} created")
        except Exception as exc:
            summary.failed("roles", f"{name or 'role'}: {exc}")


async def _import_access_groups(db: AsyncSession, document: dict[str, Any], summary: _Summary) -> None:
    """Restore access groups, resolving their roles by name so ids need not survive."""
    by_name = {role.name.casefold(): role.id for role in (await db.scalars(select(Role))).all()}
    existing = {group.name.casefold() for group in (await db.scalars(select(AccessGroup))).all()}
    for raw in document.get("access_groups") or []:
        name = str(raw.get("name") or "").strip()
        try:
            if not name:
                raise ValueError("an access group needs a name")
            if name.casefold() in existing:
                summary.skipped("access_groups", f"{name} already exists")
                continue
            wanted = [str(item).casefold() for item in (raw.get("roles") or [])]
            missing = [item for item in wanted if item not in by_name]
            if missing:
                raise ValueError(f"unknown role(s): {', '.join(missing)}")
            db.add(AccessGroup(id=new_id(), name=name, description=str(raw.get("description") or ""), role_ids_json=[by_name[item] for item in wanted]))
            existing.add(name.casefold())
            await db.flush()
            summary.created("access_groups", f"{name} created")
        except Exception as exc:
            summary.failed("access_groups", f"{name or 'access group'}: {exc}")


async def apply_import(
    db: AsyncSession,
    document: dict[str, Any],
    *,
    mode: str = "merge",
    sections: list[str] | None = None,
    user: User | None = None,
    connections: list[dict[str, Any]] | None = None,
    connectors: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a settings document. The caller owns the transaction: commit on success, roll back otherwise."""
    validate_document(document)
    if mode not in {"merge", "replace"}:
        raise BackupDocumentError("mode must be 'merge' or 'replace'")
    selected = resolve_sections(sections)
    summary = _Summary()
    needs_secret: list[str] = []
    removed: dict[str, int] | None = None

    if mode == "replace":
        removed = await reset_estate(db)

    connection_ids = (
        await _import_connections(document, list(connections or []), summary, needs_secret, dry_run)
        if "azure_connections" in selected
        else {str(item.get("display_name") or "").casefold(): item["id"] for item in (connections or [])}
    )
    connector_ids = (
        await _import_connectors(document, list(connectors or []), summary, needs_secret, dry_run)
        if "connectors" in selected
        else {str(item.get("name") or "").casefold(): item["id"] for item in (connectors or [])}
    )
    if "roles" in selected:
        await _import_roles(db, document, summary)
    if "access_groups" in selected:
        await _import_access_groups(db, document, summary)
    if "groups" in selected:
        await _import_groups(db, document, connection_ids, user, summary)
    if "virtual_machines" in selected:
        await _import_vms(db, document, connection_ids, user, summary)
    if "schedules" in selected:
        await _import_schedules(db, document, connection_ids, user, summary)
    if "notification_rules" in selected:
        await _import_rules(db, document, connector_ids, user, summary)
    if "security_policy" in selected:
        await _import_policy(db, document, summary)
    if "identity_provider" in selected:
        await _import_provider(db, document, summary)
    return summary.as_dict(mode, dry_run, needs_secret, removed)
