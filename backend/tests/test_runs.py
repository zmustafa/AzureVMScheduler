from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.auth import current_user, require_csrf
from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import Group, Schedule, ScheduleRun, SecurityPolicy, User, VirtualMachine, VmAttempt, new_id
from app.scheduling import parse_schedule_time, resolve_default_timezone, roll_up_mode, roll_up_run_status
from app.validation import validate_timezone


def test_run_status_rolls_up_from_attempt_statuses() -> None:
    assert roll_up_run_status([]) == "skipped"
    assert roll_up_run_status(["succeeded", "succeeded"]) == "succeeded"
    assert roll_up_run_status(["succeeded", "monitoring"]) == "running"
    assert roll_up_run_status(["succeeded", "failed"]) == "partially_failed"
    assert roll_up_run_status(["failed", "failed"]) == "failed"
    assert roll_up_run_status(["timed_out", "timed_out"]) == "timed_out"
    assert roll_up_run_status(["timed_out", "failed"]) == "failed"
    assert roll_up_run_status(["cancelled", "cancelled"]) == "cancelled"
    assert roll_up_run_status(["skipped", "succeeded"]) == "succeeded"


def test_mode_rolls_up_to_mixed() -> None:
    assert roll_up_mode([]) == "pending"
    assert roll_up_mode(["real", "real"]) == "real"
    assert roll_up_mode(["real", "mock"]) == "mixed"
    # An in-flight wave must not claim "mock" just because some VMs have not started yet.
    assert roll_up_mode(["pending", "pending"]) == "pending"
    assert roll_up_mode(["real", "pending"]) == "real"


def test_default_timezone_falls_back_to_new_york() -> None:
    assert resolve_default_timezone(None) == "America/New_York"
    assert resolve_default_timezone(SecurityPolicy(id=1, default_timezone="Europe/London")) == "Europe/London"
    assert resolve_default_timezone(SecurityPolicy(id=1, default_timezone="Not/AZone")) == "America/New_York"


def test_timezone_validation_requires_a_real_iana_zone() -> None:
    assert validate_timezone(" Europe/London ") == "Europe/London"
    for value in ("", "GMT+5", "Mars/Olympus"):
        with pytest.raises(ValueError):
            validate_timezone(value)


def test_daily_occurrence_is_dst_safe_across_the_spring_transition() -> None:
    before = datetime(2027, 3, 13, 12, 0, tzinfo=timezone.utc)
    first = parse_schedule_time("daily", "09:30", "America/New_York", before)
    second = parse_schedule_time("daily", "09:30", "America/New_York", first)
    assert first == datetime(2027, 3, 13, 14, 30, tzinfo=timezone.utc)
    assert second == datetime(2027, 3, 14, 13, 30, tzinfo=timezone.utc)


def test_daily_occurrence_skips_the_nonexistent_local_hour() -> None:
    before = datetime(2027, 3, 13, 5, 0, tzinfo=timezone.utc)
    result = parse_schedule_time("daily", "02:30", "America/New_York", before)
    assert result == datetime(2027, 3, 13, 7, 30, tzinfo=timezone.utc)
    assert parse_schedule_time("daily", "02:30", "America/New_York", result) == datetime(2027, 3, 15, 6, 30, tzinfo=timezone.utc)


# -- activity feed / run window API ------------------------------------

WINDOW_END = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
WINDOW_START = WINDOW_END - timedelta(hours=6)
INSIDE_AT = WINDOW_END - timedelta(hours=2)
OUTSIDE_AT = WINDOW_START - timedelta(hours=2)

VM_TEMPLATE = "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/{name}"

WINDOW = {"from": WINDOW_START.isoformat(), "to": WINDOW_END.isoformat()}


@asynccontextmanager
async def api_client(session, user: User | None = None) -> AsyncIterator[httpx.AsyncClient]:
    """Drive the ASGI app against the test session as a signed-in admin.

    Pass `user` to act as somebody else — used by the access-control tests to prove that the
    server-side walls hold for a principal with no permissions.

    The admin is persisted rather than held in memory only: rows such as `groups.created_by`
    carry a foreign key to `users`, which PostgreSQL enforces even though in-memory SQLite in
    the test fixture does not.
    """
    if user is None:
        user = await session.get(User, "00000000-0000-0000-0000-0000000000ad")
        if user is None:
            user = User(id="00000000-0000-0000-0000-0000000000ad", username="tester", role="admin")
            session.add(user)
            await session.commit()
    admin = user
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[current_user] = lambda: admin
    app.dependency_overrides[require_csrf] = lambda: admin
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def make_run(session, run_id: str, name: str, created_at: datetime, *, finished_at: datetime | None = None, status: str = "succeeded", **kwargs) -> ScheduleRun:
    run = ScheduleRun(id=run_id, schedule_name=name, created_at=created_at, started_at=created_at, finished_at=finished_at, status=status, trigger="scheduler", mode="mock", **kwargs)
    session.add(run)
    await session.commit()
    return run


async def make_attempt(session, attempt_id: str, run: ScheduleRun, vm_name: str, status: str, completed_at: datetime, sequence: int, message: str = "") -> VmAttempt:
    attempt = VmAttempt(
        id=attempt_id,
        run_id=run.id,
        vm_resource_id=VM_TEMPLATE.format(name=vm_name),
        status=status,
        mode="mock",
        message=message,
        sequence=sequence,
        claimed_at=completed_at,
        started_at=completed_at,
        completed_at=completed_at,
    )
    session.add(attempt)
    await session.commit()
    return attempt


async def seed_activity(session) -> tuple[ScheduleRun, ScheduleRun]:
    inside = await make_run(session, "11111111-1111-1111-1111-111111111111", "Morning wave", INSIDE_AT, finished_at=INSIDE_AT + timedelta(minutes=5), status="partially_failed", total_count=2, succeeded_count=1, failed_count=1)
    outside = await make_run(session, "22222222-2222-2222-2222-222222222222", "Yesterday wave", OUTSIDE_AT, finished_at=OUTSIDE_AT + timedelta(minutes=5), total_count=1, succeeded_count=1)
    await make_attempt(session, "aaaaaaaa-0000-0000-0000-000000000001", inside, "vm-alpha", "succeeded", INSIDE_AT + timedelta(minutes=1), 0)
    await make_attempt(session, "aaaaaaaa-0000-0000-0000-000000000002", inside, "vm-beta", "failed", INSIDE_AT + timedelta(minutes=2), 1, "Azure rejected the start request.")
    await make_attempt(session, "bbbbbbbb-0000-0000-0000-000000000001", outside, "vm-gamma", "succeeded", OUTSIDE_AT + timedelta(minutes=1), 0)
    return inside, outside


async def test_activity_feed_returns_waves_and_attempts_newest_first(session) -> None:
    inside, outside = await seed_activity(session)
    async with api_client(session) as client:
        response = await client.get("/api/runs/activity", params=WINDOW)
    assert response.status_code == 200
    body = response.json()
    events = body["events"]

    # Newest first: wave finish, failed attempt, succeeded attempt, wave start.
    assert [event["id"] for event in events] == [
        f"run:{inside.id}:finished",
        "attempt:aaaaaaaa-0000-0000-0000-000000000002",
        "attempt:aaaaaaaa-0000-0000-0000-000000000001",
        f"run:{inside.id}:started",
    ]
    assert [event["kind"] for event in events] == ["Wave", "Start attempt", "Start attempt", "Wave"]
    assert all(event["run_id"] == inside.id for event in events)
    assert not any(event["run_id"] == outside.id for event in events)
    assert body["truncated"] is False
    assert datetime.fromisoformat(body["from"]) == WINDOW_START
    assert datetime.fromisoformat(body["to"]) == WINDOW_END


async def test_activity_feed_maps_attempt_severity_and_carries_schedule_name(session) -> None:
    inside, _ = await seed_activity(session)
    async with api_client(session) as client:
        response = await client.get("/api/runs/activity", params=WINDOW)
    attempts = {event["title"]: event for event in response.json()["events"] if event["kind"] == "Start attempt"}

    assert attempts["vm-alpha"]["severity"] == "success"
    assert attempts["vm-beta"]["severity"] == "error"
    assert attempts["vm-beta"]["summary"] == "Azure rejected the start request."
    assert attempts["vm-beta"]["attempt_id"] == "aaaaaaaa-0000-0000-0000-000000000002"
    assert all(event["schedule_name"] == inside.schedule_name for event in attempts.values())

    waves = [event for event in response.json()["events"] if event["kind"] == "Wave"]
    assert [event["severity"] for event in waves] == ["warning", "info"]


async def test_activity_feed_truncates_to_the_requested_limit(session) -> None:
    await seed_activity(session)
    async with api_client(session) as client:
        response = await client.get("/api/runs/activity", params={**WINDOW, "limit": 2})
    body = response.json()

    assert len(body["events"]) == 2
    assert body["truncated"] is True


@pytest.mark.parametrize("to", [WINDOW_START.isoformat(), (WINDOW_START - timedelta(hours=1)).isoformat()])
async def test_activity_feed_rejects_a_window_that_does_not_move_forward(session, to: str) -> None:
    async with api_client(session) as client:
        response = await client.get("/api/runs/activity", params={"from": WINDOW_START.isoformat(), "to": to})
    assert response.status_code == 422


async def test_runs_list_filters_on_the_requested_window(session) -> None:
    inside, outside = await seed_activity(session)
    async with api_client(session) as client:
        windowed = (await client.get("/api/runs", params=WINDOW)).json()
        unfiltered = (await client.get("/api/runs")).json()

    assert windowed["total"] == 1
    assert [item["id"] for item in windowed["items"]] == [inside.id]
    assert {item["id"] for item in unfiltered["items"]} == {inside.id, outside.id}
    assert unfiltered["total"] == 2


# -- schedule attempts -------------------------------------------------

SCHEDULE_ID = "33333333-3333-3333-3333-333333333333"
MISSING_SCHEDULE_ID = "33333333-3333-3333-3333-3333333333ff"
GROUP_ID = "44444444-4444-4444-4444-444444444444"


async def make_schedule(session, schedule_id: str = SCHEDULE_ID, name: str = "Morning wave") -> Schedule:
    schedule = Schedule(id=schedule_id, name=name, schedule_type="daily", start_time="07:00", timezone="America/New_York", target_type="group", target_id=GROUP_ID)
    session.add(schedule)
    await session.commit()
    return schedule


async def make_group(session, group_id: str = GROUP_ID, name: str = "Payments") -> Group:
    group = Group(id=group_id, name=name, path=f"/{group_id}/", depth=0, sequence=1)
    session.add(group)
    await session.commit()
    return group


async def test_schedule_attempts_are_404_for_an_unknown_schedule(session) -> None:
    async with api_client(session) as client:
        response = await client.get(f"/api/schedules/{MISSING_SCHEDULE_ID}/attempts")
    assert response.status_code == 404
    assert response.json()["detail"] == "Schedule not found"


async def test_schedule_attempts_are_an_empty_list_when_the_schedule_never_ran(session) -> None:
    schedule = await make_schedule(session)
    async with api_client(session) as client:
        response = await client.get(f"/api/schedules/{schedule.id}/attempts")
    assert response.status_code == 200
    assert response.json() == []


# -- bulk VM add -------------------------------------------------------


async def test_adding_vms_is_422_when_every_resource_id_is_rejected(session) -> None:
    await make_group(session)
    async with api_client(session) as client:
        response = await client.post(f"/api/groups/{GROUP_ID}/vms", json={"vm_resource_ids": ["not-a-resource-id", "also-bad"]})
    assert response.status_code == 422
    body = response.json()
    assert body["created"] == []
    assert [item["vm_resource_id"] for item in body["errors"]] == ["not-a-resource-id", "also-bad"]


async def test_adding_vms_is_201_when_every_resource_id_is_accepted(session) -> None:
    await make_group(session)
    resource_ids = [VM_TEMPLATE.format(name="vm-alpha"), VM_TEMPLATE.format(name="vm-beta")]
    async with api_client(session) as client:
        response = await client.post(f"/api/groups/{GROUP_ID}/vms", json={"vm_resource_ids": resource_ids})
    assert response.status_code == 201
    body = response.json()
    assert [item["vm_name"] for item in body["created"]] == ["vm-alpha", "vm-beta"]
    assert body["errors"] == []


async def test_adding_vms_is_201_when_only_some_rows_are_accepted(session) -> None:
    await make_group(session)
    resource_ids = [VM_TEMPLATE.format(name="vm-alpha"), "not-a-resource-id"]
    async with api_client(session) as client:
        response = await client.post(f"/api/groups/{GROUP_ID}/vms", json={"vm_resource_ids": resource_ids})
    assert response.status_code == 201
    body = response.json()
    assert [item["vm_name"] for item in body["created"]] == ["vm-alpha"]
    assert [item["vm_resource_id"] for item in body["errors"]] == ["not-a-resource-id"]


# -- live power-state scan ---------------------------------------------

CONNECTION_ID = "33333333-3333-3333-3333-333333333333"
SUBSCRIPTION_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUBSCRIPTION_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
MISSING_VM_ID = "99999999-9999-9999-9999-999999999999"

POWER_VM_TEMPLATE = "/subscriptions/{subscription}/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/{name}"


async def make_power_vm(session, name: str, subscription_id: str, group_id: str = GROUP_ID) -> VirtualMachine:
    resource_id = POWER_VM_TEMPLATE.format(subscription=subscription_id, name=name)
    vm = VirtualMachine(
        id=new_id(),
        group_id=group_id,
        vm_resource_id=resource_id,
        normalized_resource_id=resource_id.lower(),
        vm_name=name,
        display_name=name,
        subscription_id=subscription_id,
    )
    session.add(vm)
    await session.commit()
    return vm


def patch_azure_lookup(monkeypatch: pytest.MonkeyPatch, states: AsyncMock) -> None:
    """Keep the scan entirely in-process: no connection store on disk, no Azure calls.

    The tenant deliberately has `allow_vm_start` false and `get_settings().enable_real_azure_starts`
    stays false, because the power-state scan is read-only and must not consult either gate.
    """
    connection = {"id": CONNECTION_ID, "display_name": "Zava Prod", "tenant_id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "allow_vm_start": False, "read_only": True, "disabled": False}

    async def fake_get_connection(connection_id: str | None) -> dict[str, Any] | None:
        return connection if connection_id == CONNECTION_ID else None

    async def fake_connection_labels() -> dict[str, dict[str, Any]]:
        return {CONNECTION_ID: connection}

    monkeypatch.setattr("app.main.get_connection", fake_get_connection)
    monkeypatch.setattr("app.main.connection_labels", fake_connection_labels)
    monkeypatch.setattr("app.main.read_power_states", states)


async def seed_power_estate(session) -> tuple[VirtualMachine, VirtualMachine, VirtualMachine]:
    """One application holding two VMs in subscription A and one in subscription B."""
    group = await make_group(session)
    group.azure_connection_id = CONNECTION_ID
    await session.commit()
    first = await make_power_vm(session, "vm-alpha", SUBSCRIPTION_A)
    second = await make_power_vm(session, "vm-beta", SUBSCRIPTION_A)
    third = await make_power_vm(session, "vm-gamma", SUBSCRIPTION_B)
    return first, second, third


async def test_power_state_scan_calls_azure_once_per_connection_and_subscription(session, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second, third = await seed_power_estate(session)

    async def fake_read(_connection: dict[str, Any], subscription_id: str) -> dict[str, str]:
        if subscription_id == SUBSCRIPTION_A:
            return {first.normalized_resource_id: "running", second.normalized_resource_id: "deallocated"}
        return {third.normalized_resource_id: "running"}

    states = AsyncMock(side_effect=fake_read)
    patch_azure_lookup(monkeypatch, states)

    async with api_client(session) as client:
        response = await client.post("/api/vms/power-state", json={"vm_ids": [first.id, second.id, third.id]})

    assert response.status_code == 200
    body = response.json()
    assert body["requested"] == 3
    assert body["scanned"] == 3
    assert body["failed"] == 0
    by_id = {item["vm_id"]: item for item in body["items"]}
    assert by_id[first.id]["power_state"] == "running"
    assert by_id[second.id]["power_state"] == "deallocated"
    assert by_id[third.id]["power_state"] == "running"
    assert {item["status"] for item in body["items"]} == {"ok"}
    assert by_id[first.id]["connection_name"] == "Zava Prod"
    # Two distinct (connection, subscription) pairs, so exactly two listings even though A holds two VMs.
    assert states.await_count == 2
    assert sorted(call.args[1] for call in states.await_args_list) == sorted([SUBSCRIPTION_A, SUBSCRIPTION_B])


async def test_power_state_scan_marks_a_vm_missing_from_azure_as_not_found(session, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second, _third = await seed_power_estate(session)

    async def fake_read(_connection: dict[str, Any], _subscription_id: str) -> dict[str, str]:
        return {first.normalized_resource_id: "running"}

    patch_azure_lookup(monkeypatch, AsyncMock(side_effect=fake_read))

    async with api_client(session) as client:
        response = await client.post("/api/vms/power-state", json={"vm_ids": [first.id, second.id]})

    assert response.status_code == 200
    body = response.json()
    assert body["scanned"] == 1
    assert body["failed"] == 1
    by_id = {item["vm_id"]: item for item in body["items"]}
    assert by_id[first.id]["status"] == "ok"
    assert by_id[first.id]["power_state"] == "running"
    assert by_id[second.id]["status"] == "not_found"
    assert by_id[second.id]["power_state"] is None
    assert by_id[second.id]["message"] == "Not found in Azure under this tenant"


async def test_power_state_scan_isolates_a_failing_subscription(session, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second, third = await seed_power_estate(session)

    async def fake_read(_connection: dict[str, Any], subscription_id: str) -> dict[str, str]:
        if subscription_id == SUBSCRIPTION_B:
            raise RuntimeError("Azure listing failed")
        return {first.normalized_resource_id: "running", second.normalized_resource_id: "running"}

    patch_azure_lookup(monkeypatch, AsyncMock(side_effect=fake_read))

    async with api_client(session) as client:
        response = await client.post("/api/vms/power-state", json={"vm_ids": [first.id, second.id, third.id]})

    # One broken scope must not fail the whole scan.
    assert response.status_code == 200
    body = response.json()
    assert body["scanned"] == 2
    assert body["failed"] == 1
    by_id = {item["vm_id"]: item for item in body["items"]}
    assert by_id[first.id]["status"] == "ok"
    assert by_id[second.id]["status"] == "ok"
    assert by_id[third.id]["status"] == "error"
    assert by_id[third.id]["power_state"] is None
    assert by_id[third.id]["message"]


async def test_power_state_scan_is_404_for_unknown_vm_ids(session, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed_power_estate(session)
    states = AsyncMock(return_value={})
    patch_azure_lookup(monkeypatch, states)

    async with api_client(session) as client:
        response = await client.post("/api/vms/power-state", json={"vm_ids": [MISSING_VM_ID]})

    assert response.status_code == 404
    assert response.json()["detail"] == "No matching virtual machines"
    assert states.await_count == 0


async def test_power_state_scan_is_422_for_an_empty_vm_id_list(session, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed_power_estate(session)
    patch_azure_lookup(monkeypatch, AsyncMock(return_value={}))

    async with api_client(session) as client:
        response = await client.post("/api/vms/power-state", json={"vm_ids": []})

    assert response.status_code == 422


async def test_power_state_scan_ignores_the_start_gates_because_it_is_read_only(session, monkeypatch: pytest.MonkeyPatch) -> None:
    first, _second, _third = await seed_power_estate(session)

    async def fake_read(_connection: dict[str, Any], _subscription_id: str) -> dict[str, str]:
        return {first.normalized_resource_id: "running"}

    patch_azure_lookup(monkeypatch, AsyncMock(side_effect=fake_read))
    # The scan must succeed even though real starts are off server-wide and the tenant
    # (see patch_azure_lookup) has `allow_vm_start` false: neither gate is consulted.
    assert get_settings().enable_real_azure_starts is False

    async with api_client(session) as client:
        response = await client.post("/api/vms/power-state", json={"vm_ids": [first.id]})

    assert response.status_code == 200
    body = response.json()
    assert body["scanned"] == 1
    assert body["items"][0]["status"] == "ok"
    assert body["items"][0]["power_state"] == "running"
    assert body["checked_at"]


# -- VM connection resolution in the payload ---------------------------

APP_ID = "55555555-5555-5555-5555-555555555555"
RING_ID = "66666666-6666-6666-6666-666666666666"
APP_CONNECTION_ID = "77777777-7777-7777-7777-777777777777"
VM_CONNECTION_ID = "88888888-8888-8888-8888-888888888888"
APP_TENANT_ID = "aaaa1111-aaaa-1111-aaaa-111111111111"
VM_TENANT_ID = "bbbb2222-bbbb-2222-bbbb-222222222222"


def patch_connection_registry(monkeypatch: pytest.MonkeyPatch, entries: dict[str, dict[str, Any]]) -> None:
    """Serve connection labels from memory so the on-disk encrypted registry is never touched."""

    async def fake_connection_labels() -> dict[str, dict[str, Any]]:
        return entries

    monkeypatch.setattr("app.main.connection_labels", fake_connection_labels)


def connection_entry(connection_id: str, display_name: str, tenant_id: str) -> dict[str, Any]:
    return {"id": connection_id, "display_name": display_name, "tenant_id": tenant_id, "allow_vm_start": False, "read_only": True, "disabled": False}


async def seed_ring_vm(session, *, vm_connection_id: str | None = None) -> VirtualMachine:
    """An application carrying a connection, a ring beneath it, and one VM inside the ring."""
    application = Group(id=APP_ID, name="Payments", path=f"/{APP_ID}/", depth=0, sequence=1, azure_connection_id=APP_CONNECTION_ID)
    ring = Group(id=RING_ID, name="Ring 1", parent_id=APP_ID, path=f"/{APP_ID}/{RING_ID}/", depth=1, sequence=1)
    session.add_all([application, ring])
    await session.commit()
    vm = await make_power_vm(session, "vm-inherits", SUBSCRIPTION_A, group_id=RING_ID)
    vm.azure_connection_id = vm_connection_id
    await session.commit()
    return vm


async def fetch_vm_payloads(session, path: str) -> dict[str, dict[str, Any]]:
    async with api_client(session) as client:
        response = await client.get(path)
    assert response.status_code == 200
    return {item["id"]: item for item in response.json()["items"]}


async def test_vm_payload_inherits_the_effective_tenant_id_from_the_application(session, monkeypatch: pytest.MonkeyPatch) -> None:
    vm = await seed_ring_vm(session)
    patch_connection_registry(monkeypatch, {APP_CONNECTION_ID: connection_entry(APP_CONNECTION_ID, "Zava Prod", APP_TENANT_ID)})

    for path in ("/api/vms", f"/api/groups/{RING_ID}/vms"):
        payload = (await fetch_vm_payloads(session, path))[vm.id]
        # The VM has no override of its own, so only the effective fields are populated.
        assert payload["connection_name"] is None
        assert payload["connection_tenant_id"] is None
        assert payload["effective_connection_id"] == APP_CONNECTION_ID
        assert payload["effective_connection_name"] == "Zava Prod"
        assert payload["effective_connection_tenant_id"] == APP_TENANT_ID


async def test_vm_payload_uses_its_own_connection_override_for_the_effective_tenant_id(session, monkeypatch: pytest.MonkeyPatch) -> None:
    vm = await seed_ring_vm(session, vm_connection_id=VM_CONNECTION_ID)
    patch_connection_registry(
        monkeypatch,
        {
            APP_CONNECTION_ID: connection_entry(APP_CONNECTION_ID, "Zava Prod", APP_TENANT_ID),
            VM_CONNECTION_ID: connection_entry(VM_CONNECTION_ID, "Fabrikam Test", VM_TENANT_ID),
        },
    )

    for path in ("/api/vms", f"/api/groups/{RING_ID}/vms"):
        payload = (await fetch_vm_payloads(session, path))[vm.id]
        assert payload["connection_name"] == "Fabrikam Test"
        assert payload["connection_tenant_id"] == VM_TENANT_ID
        assert payload["effective_connection_id"] == VM_CONNECTION_ID
        assert payload["effective_connection_name"] == "Fabrikam Test"
        assert payload["effective_connection_tenant_id"] == VM_TENANT_ID


async def test_vm_payload_reports_a_blank_registry_tenant_id_as_none(session, monkeypatch: pytest.MonkeyPatch) -> None:
    vm = await seed_ring_vm(session)
    # A pasted-token connection has no tenant id; the payload must not leak an empty string.
    patch_connection_registry(monkeypatch, {APP_CONNECTION_ID: connection_entry(APP_CONNECTION_ID, "Pasted token", "")})

    payload = (await fetch_vm_payloads(session, "/api/vms"))[vm.id]
    assert payload["effective_connection_id"] == APP_CONNECTION_ID
    assert payload["effective_connection_name"] == "Pasted token"
    assert payload["effective_connection_tenant_id"] is None

