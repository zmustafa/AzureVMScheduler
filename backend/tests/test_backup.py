from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.backup import (
    SECRET_KEYS,
    BackupDocumentError,
    apply_import,
    build_export,
    reset_estate,
    validate_document,
)
from app.hierarchy import next_sequence, recompute_subtree
from app.models import AuditLog, Group, NotificationRule, Schedule, ScheduleRun, User, VirtualMachine, VmAttempt, new_id


VM_TEMPLATE = "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/{name}"

CONNECTION_ID = "11111111-1111-1111-1111-111111111111"
CONNECTOR_ID = "22222222-2222-2222-2222-222222222222"

# Deliberately dirty inputs: the exporter must allow-list rather than blacklist.
CONNECTIONS = [
    {
        "id": CONNECTION_ID,
        "display_name": "Zava Prod",
        "auth_method": "service_principal",
        "tenant_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "client_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "allow_vm_start": True,
        "allow_vm_stop": True,
        "read_only": False,
        "is_default": True,
        "disabled": False,
        "client_secret": "super-secret-value",
        "certificate_pem": "-----BEGIN PRIVATE KEY-----",
        "access_token_json": '{"accessToken":"tok"}',
        "has_client_secret": True,
        "client_secret_hint": "••••••••",
    }
]
CONNECTORS = [
    {
        "id": CONNECTOR_ID,
        "name": "Ops Teams channel",
        "type": "teams",
        "mode": "webhook",
        "disabled": False,
        "config": {"webhook_url": "https://zava.webhook.office.com/secret-hook", "webhook_url_set": True},
    }
]


async def make_group(session, name: str, parent: Group | None = None, **kwargs) -> Group:
    group = Group(id=new_id(), name=name, parent_id=parent.id if parent else None, sequence=await next_sequence(session, parent.id if parent else None), **kwargs)
    session.add(group)
    await session.flush()
    await recompute_subtree(session, group)
    await session.commit()
    return group


async def make_vm(session, group: Group, name: str, **kwargs) -> VirtualMachine:
    resource_id = VM_TEMPLATE.format(name=name)
    vm = VirtualMachine(id=new_id(), group_id=group.id, vm_resource_id=resource_id, normalized_resource_id=resource_id.lower(), vm_name=name, display_name=name, **kwargs)
    session.add(vm)
    await session.commit()
    return vm


async def make_schedule(session, target_type: str, target_id: str, name: str, start_time: str = "07:00", **kwargs) -> Schedule:
    schedule = Schedule(id=new_id(), name=name, schedule_type="daily", start_time=start_time, timezone="America/New_York", target_type=target_type, target_id=target_id, **kwargs)
    session.add(schedule)
    await session.commit()
    return schedule


async def build_estate(session) -> dict[str, object]:
    """An application with one ring, VMs on the ring, and both a group- and a VM-targeted schedule."""
    application = await make_group(session, "Payments", description="Payments platform", azure_connection_id=CONNECTION_ID)
    ring = await make_group(session, "Ring 1", application)
    first = await make_vm(session, ring, "vm-pay-01", notes="primary")
    second = await make_vm(session, ring, "vm-pay-02", enabled=False)
    group_schedule = await make_schedule(session, "group", application.id, "Payments wave", stagger_seconds=30, azure_connection_id=CONNECTION_ID)
    vm_schedule = await make_schedule(session, "vm", second.id, "Late starter", start_time="09:15")
    session.add(NotificationRule(id=new_id(), name="Failures", event_types=["run.failed"], connector_ids=[CONNECTOR_ID], scope_group_id=application.id))
    await session.commit()
    return {"application": application, "ring": ring, "vms": [first, second], "schedules": [group_schedule, vm_schedule]}


async def export(session) -> dict:
    return await build_export(session, CONNECTIONS, CONNECTORS, app_version="test")


def walk_keys(node: object) -> list[str]:
    if isinstance(node, dict):
        return [key for key in node] + [item for value in node.values() for item in walk_keys(value)]
    if isinstance(node, list):
        return [item for value in node for item in walk_keys(value)]
    return []


async def test_export_never_carries_secret_values_or_keys(session) -> None:
    await build_estate(session)
    document = await export(session)
    serialized = json.dumps(document)

    for secret in ("super-secret-value", "-----BEGIN PRIVATE KEY-----", '{"accessToken":"tok"}', "https://zava.webhook.office.com/secret-hook"):
        assert secret not in serialized
    # secret_fields only names the fields; no payload dict may be keyed by a secret.
    assert not SECRET_KEYS.intersection(walk_keys(document))
    assert document["azure_connections"][0]["secret_fields"] == ["client_secret"]
    assert document["connectors"][0]["secret_fields"] == ["webhook_url"]
    assert document["connectors"][0]["config"] == {}


async def test_export_uses_names_not_ids(session) -> None:
    await build_estate(session)
    document = await export(session)

    assert document["format"] == "azure-vm-scheduler.settings"
    assert document["version"] == 1
    assert ["Payments", "Ring 1"] in [item["name_path"] for item in document["groups"]]
    assert document["virtual_machines"][0]["group_path"] == ["Payments", "Ring 1"]
    targets = {item["name"]: item["target"] for item in document["schedules"]}
    assert targets["Payments wave"] == {"type": "group", "path": ["Payments"]}
    assert targets["Late starter"]["type"] == "vm"
    assert targets["Late starter"]["vm_resource_id"].endswith("vm-pay-02")
    assert document["notification_rules"][0]["connectors"] == ["Ops Teams channel"]
    assert document["notification_rules"][0]["scope_group_path"] == ["Payments"]
    # Operational history and credentials are never part of a settings document.
    assert not {"users", "sessions", "audit_logs", "runs", "vm_attempts", "import_batches"} & set(document)


async def test_export_import_round_trips_the_estate(session) -> None:
    await build_estate(session)
    document = await export(session)
    await reset_estate(session)
    await session.commit()

    summary = await apply_import(session, document, sections=["groups", "virtual_machines", "schedules", "notification_rules"], connections=CONNECTIONS, connectors=CONNECTORS)
    await session.commit()

    assert summary["failed"] == 0
    assert summary["sections"]["groups"]["created"] == 2
    assert summary["sections"]["virtual_machines"]["created"] == 2
    assert summary["sections"]["schedules"]["created"] == 2

    ring = await session.scalar(select(Group).where(Group.name == "Ring 1"))
    assert ring is not None and ring.depth == 1
    vms = (await session.scalars(select(VirtualMachine).order_by(VirtualMachine.vm_name))).all()
    assert [item.vm_name for item in vms] == ["vm-pay-01", "vm-pay-02"]
    assert [item.enabled for item in vms] == [True, False]
    assert vms[0].group_id == ring.id
    schedules = (await session.scalars(select(Schedule).order_by(Schedule.name))).all()
    assert {item.target_type for item in schedules} == {"group", "vm"}
    group_schedule = next(item for item in schedules if item.target_type == "group")
    assert group_schedule.stagger_seconds == 30
    assert group_schedule.azure_connection_id == CONNECTION_ID
    vm_schedule = next(item for item in schedules if item.target_type == "vm")
    assert vm_schedule.target_id == vms[1].id
    rule = await session.scalar(select(NotificationRule))
    assert rule is not None and rule.connector_ids == [CONNECTOR_ID] and rule.scope_group_id is not None


async def test_stop_settings_survive_an_export_import_round_trip(session) -> None:
    """The exporter allow-lists fields, so every stop setting needs to be carried deliberately."""
    application = await make_group(session, "Payments", never_stop=True)
    ring = await make_group(session, "Ring 1", application)
    await make_vm(session, ring, "vm-pay-01", never_stop=True)
    await make_schedule(session, "group", ring.id, "Evening shutdown", start_time="19:00", action="stop", stop_mode="power_off", ring_order="reverse")
    # Same target and time as the stop, so the importer must treat action as part of identity.
    await make_schedule(session, "group", ring.id, "Evening start", start_time="19:00", action="start")

    document = await export(session)
    assert [item["name"] for item in document["schedules"] if item["action"] == "stop"] == ["Evening shutdown"]

    await reset_estate(session)
    await session.commit()
    summary = await apply_import(session, document, sections=["groups", "virtual_machines", "schedules"], connections=CONNECTIONS, connectors=CONNECTORS)
    await session.commit()

    assert summary["failed"] == 0
    assert summary["sections"]["schedules"]["created"] == 2

    restored = await session.scalar(select(Group).where(Group.name == "Payments"))
    assert restored.never_stop is True
    vm = await session.scalar(select(VirtualMachine))
    assert vm.never_stop is True
    stop = await session.scalar(select(Schedule).where(Schedule.action == "stop"))
    assert (stop.stop_mode, stop.ring_order) == ("power_off", "reverse")


async def test_connection_export_carries_the_stop_permission(session) -> None:
    """allow_vm_stop is a safety gate; losing it on restore would silently re-permit stops."""
    await build_estate(session)
    document = await export(session)
    assert document["azure_connections"][0]["allow_vm_stop"] is True


async def test_merge_import_is_idempotent(session) -> None:
    await build_estate(session)
    document = await export(session)
    await reset_estate(session)
    await session.commit()

    sections = ["groups", "virtual_machines", "schedules", "notification_rules"]
    first = await apply_import(session, document, sections=sections, connections=CONNECTIONS, connectors=CONNECTORS)
    await session.commit()
    second = await apply_import(session, document, sections=sections, connections=CONNECTIONS, connectors=CONNECTORS)
    await session.commit()

    assert first["created"] > 0
    assert second["created"] == 0
    assert second["failed"] == 0
    assert second["skipped"] == first["created"] + first["skipped"]
    assert await session.scalar(select(func.count()).select_from(Group)) == 2
    assert await session.scalar(select(func.count()).select_from(VirtualMachine)) == 2
    assert await session.scalar(select(func.count()).select_from(Schedule)) == 2


async def test_import_preview_writes_nothing(session) -> None:
    await build_estate(session)
    document = await export(session)
    await reset_estate(session)
    await session.commit()

    summary = await apply_import(session, document, sections=["groups", "virtual_machines"], connections=CONNECTIONS, connectors=CONNECTORS, dry_run=True)
    await session.rollback()

    assert summary["dry_run"] is True
    assert summary["sections"]["groups"]["created"] == 2
    assert await session.scalar(select(func.count()).select_from(Group)) == 0


async def test_replace_clears_the_old_estate_but_keeps_users_and_audit(session) -> None:
    await build_estate(session)
    document = await export(session)
    session.add(User(id=new_id(), username="auditor", role="auditor"))
    session.add(AuditLog(id=new_id(), action="settings.exported", target_type="backup", target_id=None))
    stale = await make_group(session, "Retired application")
    await make_vm(session, stale, "vm-old-01")
    await session.commit()

    summary = await apply_import(session, document, mode="replace", sections=["groups", "virtual_machines", "schedules"], connections=CONNECTIONS, connectors=CONNECTORS)
    await session.commit()

    assert summary["removed"]["groups_removed"] == 3
    assert summary["failed"] == 0
    names = set((await session.scalars(select(Group.name))).all())
    assert "Retired application" not in names
    assert names == {"Payments", "Ring 1"}
    assert await session.scalar(select(func.count()).select_from(User)) == 1
    assert await session.scalar(select(func.count()).select_from(AuditLog)) == 1


async def test_reset_estate_removes_runs_but_keeps_identity(session) -> None:
    estate = await build_estate(session)
    schedule = estate["schedules"][0]  # type: ignore[index]
    run = ScheduleRun(id=new_id(), schedule_id=schedule.id, schedule_name=schedule.name)
    session.add(run)
    session.add(VmAttempt(id=new_id(), schedule_id=schedule.id, run_id=run.id, vm_resource_id="x"))
    session.add(User(id=new_id(), username="keeper", role="admin"))
    session.add(AuditLog(id=new_id(), action="estate.reset", target_type="backup", target_id=None))
    await session.commit()

    removed = await reset_estate(session)
    await session.commit()

    assert removed == {"groups_removed": 2, "vms_removed": 2, "schedules_removed": 2, "runs_removed": 1}
    assert await session.scalar(select(func.count()).select_from(VmAttempt)) == 0
    assert await session.scalar(select(func.count()).select_from(ScheduleRun)) == 0
    assert await session.scalar(select(func.count()).select_from(User)) == 1
    assert await session.scalar(select(func.count()).select_from(AuditLog)) == 1
    assert await session.scalar(select(func.count()).select_from(NotificationRule)) == 1


@pytest.mark.parametrize(
    "document",
    [
        {"format": "something.else", "version": 1},
        {"format": "azureops.settings", "version": 99},
        {"format": "azureops.settings"},
        ["not", "an", "object"],
    ],
)
def test_import_rejects_foreign_documents(document) -> None:
    with pytest.raises(BackupDocumentError):
        validate_document(document)


async def test_import_rejects_unknown_sections(session) -> None:
    with pytest.raises(BackupDocumentError):
        await apply_import(session, {"format": "azureops.settings", "version": 1}, sections=["users"])


async def test_import_records_a_three_level_group_path_as_a_failed_row(session) -> None:
    """A legacy document with nested rings must fail that row, not flatten it and not blow up."""
    document = {
        "format": "azureops.settings",
        "version": 1,
        "groups": [
            {"name_path": ["Payments"]},
            {"name_path": ["Payments", "Ring 1"]},
            {"name_path": ["Payments", "Ring 1", "Ring 1a"]},
        ],
    }

    summary = await apply_import(session, document, sections=["groups"])
    await session.commit()

    section = summary["sections"]["groups"]
    assert section["created"] == 2
    assert section["failed"] == 1
    assert summary["failed"] == 1
    failure = next(item for item in section["details"] if item["outcome"] == "failed")
    assert "Payments / Ring 1 / Ring 1a" in failure["message"]
    assert "ring cannot contain another ring" in failure["message"]
    names = set((await session.scalars(select(Group.name))).all())
    assert names == {"Payments", "Ring 1"}


async def test_reset_estate_endpoint_requires_the_exact_confirm_string(session) -> None:
    from fastapi import HTTPException

    from app.main import estate_reset
    from app.schemas import EstateResetRequest

    await build_estate(session)
    admin = User(id=new_id(), username="root", role="admin")

    for value in ("delete", "DELETE ", "", "yes"):
        with pytest.raises(HTTPException) as raised:
            await estate_reset(EstateResetRequest(confirm=value), admin, session)
        assert raised.value.status_code == 422
    assert await session.scalar(select(func.count()).select_from(Group)) == 2
