from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from app.auth import current_user, require_csrf
from app.database import get_db
from app.main import app
from app.models import (
    Group,
    Schedule,
    ScheduleRun,
    SecurityPolicy,
    User,
    VirtualMachine,
    VmAttempt,
    new_id,
)
from app.overview import build_overview
from app.scheduling import utcnow

# The readiness checks and the stuck-run cutoff are measured against the real clock inside
# build_overview, so every fixture time is anchored to "now" rather than a fixed calendar date.
NOW = utcnow()
WINDOW_END = NOW
WINDOW_START = NOW - timedelta(hours=6)
INSIDE_AT = NOW - timedelta(hours=2)
PREVIOUS_AT = NOW - timedelta(hours=8)
OUTSIDE_AT = NOW - timedelta(hours=30)

MONITOR_TIMEOUT_SECONDS = 300

VM_TEMPLATE = "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/{name}"

CONNECTION_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
OTHER_CONNECTION_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


# -- harness -----------------------------------------------------------


@asynccontextmanager
async def api_client(session) -> AsyncIterator[httpx.AsyncClient]:
    """Drive the ASGI app against the in-memory session as a signed-in admin."""
    admin = User(id="00000000-0000-0000-0000-0000000000ad", username="tester", role="admin")
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[current_user] = lambda: admin
    app.dependency_overrides[require_csrf] = lambda: admin
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def overview(
    session,
    *,
    connections: list[dict[str, Any]] | None = None,
    real_starts_enabled: bool = True,
    monitor_timeout_seconds: int = MONITOR_TIMEOUT_SECONDS,
    start: datetime = WINDOW_START,
    end: datetime = WINDOW_END,
) -> dict[str, Any]:
    # SecurityPolicy column defaults only fire on INSERT, so the grace has to be set explicitly.
    policy = SecurityPolicy(id=1, schedule_missed_grace_seconds=300)
    return await build_overview(
        session,
        start,
        end,
        connections=connections or [],
        real_starts_enabled=real_starts_enabled,
        policy=policy,
        monitor_timeout_seconds=monitor_timeout_seconds,
    )


def check_ids(result: dict[str, Any]) -> list[str]:
    return [item["id"] for item in result["readiness"]]


def check_with_prefix(result: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    return next((item for item in result["readiness"] if item["id"].startswith(prefix)), None)


# -- seeding helpers ---------------------------------------------------


async def make_application(session, name: str = "Payments", sequence: int = 1, *, enabled: bool = True, connection_id: str | None = None) -> Group:
    group = Group(id=new_id(), parent_id=None, name=name, depth=0, sequence=sequence, enabled=enabled, azure_connection_id=connection_id)
    group.path = f"/{group.id}/"
    session.add(group)
    await session.commit()
    return group


async def make_ring(session, application: Group, name: str, sequence: int = 1, *, enabled: bool = True) -> Group:
    group = Group(id=new_id(), parent_id=application.id, name=name, depth=1, sequence=sequence, enabled=enabled)
    group.path = f"{application.path}{group.id}/"
    session.add(group)
    await session.commit()
    return group


async def make_vm(
    session,
    group: Group,
    name: str,
    *,
    enabled: bool = True,
    power_state: str = "",
    power_state_at: datetime | None = None,
    connection_id: str | None = None,
) -> VirtualMachine:
    resource_id = VM_TEMPLATE.format(name=name)
    vm = VirtualMachine(
        id=new_id(),
        group_id=group.id,
        vm_resource_id=resource_id,
        normalized_resource_id=resource_id.lower(),
        vm_name=name,
        display_name=name,
        subscription_id="12345678-1234-1234-1234-123456789abc",
        enabled=enabled,
        last_power_state=power_state,
        last_power_state_at=power_state_at,
        azure_connection_id=connection_id,
    )
    session.add(vm)
    await session.commit()
    return vm


async def make_schedule(
    session,
    name: str,
    target: Group | VirtualMachine,
    *,
    enabled: bool = True,
    next_run_at: datetime | None = None,
    stagger_seconds: int = 0,
) -> Schedule:
    schedule = Schedule(
        id=new_id(),
        name=name,
        schedule_type="daily",
        start_time="07:00",
        timezone="America/New_York",
        target_type="group" if isinstance(target, Group) else "vm",
        target_id=target.id,
        enabled=enabled,
        next_run_at=next_run_at,
        stagger_seconds=stagger_seconds,
    )
    session.add(schedule)
    await session.commit()
    return schedule


async def make_run(
    session,
    created_at: datetime,
    *,
    status: str = "succeeded",
    finished: bool = True,
    schedule: Schedule | None = None,
    total_count: int = 1,
    succeeded_count: int = 1,
    failed_count: int = 0,
) -> ScheduleRun:
    run = ScheduleRun(
        id=new_id(),
        schedule_id=schedule.id if schedule else None,
        schedule_name=schedule.name if schedule else "Ad-hoc wave",
        scheduled_for=created_at,
        created_at=created_at,
        started_at=created_at,
        finished_at=created_at + timedelta(minutes=5) if finished else None,
        status=status,
        trigger="scheduler",
        mode="mock",
        total_count=total_count,
        succeeded_count=succeeded_count,
        failed_count=failed_count,
    )
    session.add(run)
    await session.commit()
    return run


async def make_attempt(
    session,
    run: ScheduleRun,
    vm_name: str,
    status: str,
    completed_at: datetime,
    *,
    sequence: int = 0,
    message: str = "",
) -> VmAttempt:
    attempt = VmAttempt(
        id=new_id(),
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


# -- KPIs and trend ----------------------------------------------------


async def test_kpis_split_the_window_from_the_equally_sized_previous_period(session) -> None:
    await make_run(session, INSIDE_AT, status="succeeded", total_count=3, succeeded_count=3)
    inside_failed = await make_run(session, INSIDE_AT + timedelta(minutes=10), status="failed", total_count=2, succeeded_count=0, failed_count=2)
    previous_failed = await make_run(session, PREVIOUS_AT, status="partially_failed", total_count=2, succeeded_count=1, failed_count=1)
    await make_run(session, OUTSIDE_AT, status="failed", total_count=9, succeeded_count=9)
    await make_attempt(session, inside_failed, "vm-alpha", "failed", INSIDE_AT + timedelta(minutes=11))
    await make_attempt(session, inside_failed, "vm-beta", "timed_out", INSIDE_AT + timedelta(minutes=12), sequence=1)
    await make_attempt(session, previous_failed, "vm-gamma", "failed", PREVIOUS_AT + timedelta(minutes=1))

    kpis = (await overview(session))["kpis"]

    assert kpis["runs"] == {"current": 2, "previous": 1, "change": 1}
    assert kpis["failed_runs"] == {"current": 1, "previous": 1, "change": 0}
    assert kpis["failed_attempts"] == {"current": 2, "previous": 1, "change": 1}
    assert kpis["vms_started"] == {"current": 3, "previous": 1, "change": 2}


async def test_window_reports_the_previous_period_boundary(session) -> None:
    window = (await overview(session))["window"]

    assert window["from"] == WINDOW_START
    assert window["to"] == WINDOW_END
    assert window["previous_from"] == WINDOW_START - (WINDOW_END - WINDOW_START)


async def test_trend_always_has_fourteen_buckets_that_account_for_every_run(session) -> None:
    await make_run(session, WINDOW_START + timedelta(minutes=1))
    await make_run(session, INSIDE_AT)
    await make_run(session, WINDOW_END - timedelta(minutes=1))
    await make_run(session, PREVIOUS_AT)  # previous period: must not appear in the trend

    result = await overview(session)

    assert len(result["trend"]) == 14
    assert sum(bucket["runs"] for bucket in result["trend"]) == 3
    assert result["trend"][0]["start"] == WINDOW_START
    assert all(bucket["start"] < WINDOW_END for bucket in result["trend"])


# -- readiness ---------------------------------------------------------


async def test_mock_mode_warning_appears_only_when_real_starts_are_disabled(session) -> None:
    mocked = await overview(session, real_starts_enabled=False)
    real = await overview(session, real_starts_enabled=True)

    mock_check = next(item for item in mocked["readiness"] if item["id"] == "mock_mode")
    assert mock_check["severity"] == "warning"
    assert mock_check["link"] == "/settings"
    assert "mock_mode" not in check_ids(real)


async def test_expired_and_expiring_tenant_credentials_raise_readiness_checks(session) -> None:
    connections = [
        {
            "id": CONNECTION_ID,
            "display_name": "Zava Prod",
            "auth_method": "az_cli_token",
            "allow_vm_start": True,
            "read_only": False,
            "disabled": False,
            "token_expires_at": (NOW - timedelta(hours=1)).isoformat(),
        },
        {
            "id": OTHER_CONNECTION_ID,
            "display_name": "Zava Test",
            "auth_method": "az_cli_token",
            "allow_vm_start": True,
            "read_only": False,
            "disabled": False,
            "token_expires_at": (NOW + timedelta(minutes=20)).isoformat(),
        },
    ]

    result = await overview(session, connections=connections)

    expired = check_with_prefix(result, "token_expired:")
    expiring = check_with_prefix(result, "token_expiring:")
    assert expired is not None and expired["id"] == f"token_expired:{CONNECTION_ID}"
    assert expired["severity"] == "error"
    assert "Zava Prod" in expired["title"]
    assert expiring is not None and expiring["id"] == f"token_expiring:{OTHER_CONNECTION_ID}"
    assert expiring["severity"] == "warning"
    assert expiring["link"] == "/settings/tenants"


async def test_valid_tenant_credentials_raise_no_token_checks(session) -> None:
    connections = [{
        "id": CONNECTION_ID,
        "display_name": "Zava Prod",
        "auth_method": "az_cli_token",
        "allow_vm_start": True,
        "read_only": False,
        "disabled": False,
        "token_expires_at": (NOW + timedelta(days=3)).isoformat(),
    }]

    result = await overview(session, connections=connections)

    assert check_with_prefix(result, "token_expired:") is None
    assert check_with_prefix(result, "token_expiring:") is None


@pytest.mark.parametrize("auth_method", ["service_principal", "service_principal_cert", "azure_cli", "default_chain"])
async def test_only_a_pasted_token_can_be_reported_as_expired(session, auth_method: str) -> None:
    """`token_expires_at` describes a pasted CLI token and nothing else.

    A connection that was once a pasted token and was later edited to a service principal keeps the
    old timestamp in its record, so applying the check to every auth method reported a perfectly
    healthy app registration as expired — permanently, and as a blocking error.
    """
    connections = [{
        "id": CONNECTION_ID,
        "display_name": "Zava App Registration",
        "auth_method": auth_method,
        "allow_vm_start": True,
        "read_only": False,
        "disabled": False,
        "token_expires_at": (NOW - timedelta(days=30)).isoformat(),
    }]

    result = await overview(session, connections=connections)

    assert check_with_prefix(result, "token_expired:") is None
    assert check_with_prefix(result, "token_expiring:") is None


async def test_read_only_tenant_is_flagged_only_when_a_schedule_depends_on_it(session) -> None:
    application = await make_application(session, connection_id=CONNECTION_ID)
    ring = await make_ring(session, application, "Ring 1")
    await make_vm(session, ring, "vm-alpha")
    await make_schedule(session, "Ring 1 wave", ring)
    # A second tenant exists but nothing resolves to it, so it must stay quiet.
    unused_application = await make_application(session, "Retired", sequence=2, connection_id=OTHER_CONNECTION_ID)
    await make_vm(session, unused_application, "vm-orphan")

    connections = [
        {"id": CONNECTION_ID, "display_name": "Zava Prod", "allow_vm_start": True, "read_only": True, "disabled": False},
        {"id": OTHER_CONNECTION_ID, "display_name": "Zava Retired", "allow_vm_start": True, "read_only": True, "disabled": False},
    ]
    result = await overview(session, connections=connections)

    read_only = [item for item in result["readiness"] if item["id"].startswith("tenant_read_only:")]
    assert [item["id"] for item in read_only] == [f"tenant_read_only:{CONNECTION_ID}"]
    assert read_only[0]["severity"] == "error"


async def test_enabled_schedule_that_resolves_to_no_vms_is_flagged(session) -> None:
    application = await make_application(session)
    empty_ring = await make_ring(session, application, "Ring 1")
    empty = await make_schedule(session, "Empty wave", empty_ring)
    populated_ring = await make_ring(session, application, "Ring 2", sequence=2)
    await make_vm(session, populated_ring, "vm-alpha")
    populated = await make_schedule(session, "Populated wave", populated_ring)

    result = await overview(session)

    assert f"empty_schedule:{empty.id}" in check_ids(result)
    assert f"empty_schedule:{populated.id}" not in check_ids(result)


async def test_disabled_schedules_never_raise_readiness_checks(session) -> None:
    application = await make_application(session)
    ring = await make_ring(session, application, "Ring 1")
    disabled = await make_schedule(session, "Retired wave", ring, enabled=False)

    result = await overview(session)

    assert f"empty_schedule:{disabled.id}" not in check_ids(result)


async def test_runs_that_never_finished_raise_the_stuck_runs_check(session) -> None:
    await make_run(session, INSIDE_AT, finished=True)
    assert "stuck_runs" not in check_ids(await overview(session))

    # Older than monitor_timeout_seconds * 2 with no finished_at: the monitor can never close it.
    await make_run(session, NOW - timedelta(hours=2), finished=False, status="running")

    result = await overview(session)
    stuck = next(item for item in result["readiness"] if item["id"] == "stuck_runs")
    assert stuck["severity"] == "warning"
    assert stuck["link"] == "/runs?status=running"
    assert result["kpis"]["running_runs"] == 1


# -- coverage ----------------------------------------------------------


async def test_uncovered_vms_are_counted_in_full_and_sampled_to_eight(session) -> None:
    application = await make_application(session)
    ring = await make_ring(session, application, "Ring 1")
    for index in range(10):
        await make_vm(session, ring, f"vm-uncovered-{index:02d}")

    coverage = (await overview(session))["coverage"]

    assert coverage["uncovered_vm_count"] == 10
    assert len(coverage["uncovered_sample"]) == 8
    assert all(item["group_path"] == "Payments / Ring 1" for item in coverage["uncovered_sample"])


async def test_vms_owned_by_an_ancestor_schedule_are_not_uncovered(session) -> None:
    application = await make_application(session)
    ring = await make_ring(session, application, "Ring 1")
    await make_vm(session, ring, "vm-alpha")
    await make_vm(session, ring, "vm-beta")
    # The schedule sits on the application, so both ring members inherit it.
    await make_schedule(session, "Application wave", application)

    coverage = (await overview(session))["coverage"]

    assert coverage["uncovered_vm_count"] == 0
    assert coverage["uncovered_sample"] == []


async def test_coverage_lists_applications_without_schedules_and_empty_schedules(session) -> None:
    covered = await make_application(session, "Payments")
    covered_ring = await make_ring(session, covered, "Ring 1")
    await make_vm(session, covered_ring, "vm-alpha")
    await make_schedule(session, "Ring 1 wave", covered_ring)
    empty_ring = await make_ring(session, covered, "Ring 2", sequence=2)
    empty = await make_schedule(session, "Empty wave", empty_ring)

    bare = await make_application(session, "Billing", sequence=2)
    await make_vm(session, bare, "vm-billing")

    coverage = (await overview(session))["coverage"]

    assert coverage["applications_without_schedules"] == [{"id": bare.id, "name": "Billing", "vm_count": 1}]
    assert coverage["empty_schedules"] == [{"id": empty.id, "name": "Empty wave", "action": "start"}]


async def test_disabled_vms_inside_a_scheduled_ring_are_reported_separately(session) -> None:
    application = await make_application(session)
    ring = await make_ring(session, application, "Ring 1")
    await make_vm(session, ring, "vm-alpha")
    await make_vm(session, ring, "vm-beta", enabled=False)
    await make_schedule(session, "Ring 1 wave", ring)

    coverage = (await overview(session))["coverage"]

    assert coverage["uncovered_vm_count"] == 0
    assert coverage["disabled_in_scheduled_ring"] == 1


# -- rollout plan ------------------------------------------------------


async def test_rollout_plan_orders_applications_and_waves_by_start_time(session) -> None:
    payments = await make_application(session, "Payments", sequence=1)
    first_ring = await make_ring(session, payments, "Ring 1", sequence=1)
    second_ring = await make_ring(session, payments, "Ring 2", sequence=2)
    await make_vm(session, first_ring, "vm-p1")
    await make_vm(session, first_ring, "vm-p2")
    await make_vm(session, second_ring, "vm-p3")
    # Deliberately created out of chronological order to prove the plan sorts by time.
    await make_schedule(session, "Ring 2 wave", second_ring, next_run_at=NOW + timedelta(hours=2))
    await make_schedule(session, "Ring 1 wave", first_ring, next_run_at=NOW + timedelta(hours=1), stagger_seconds=60)

    billing = await make_application(session, "Billing", sequence=2)
    await make_vm(session, billing, "vm-b1")
    await make_schedule(session, "Billing wave", billing, next_run_at=NOW + timedelta(minutes=30))

    plan = (await overview(session))["rollout_plan"]

    # Billing starts first even though its application sequence is higher.
    assert [item["name"] for item in plan] == ["Billing", "Payments"]
    payments_plan = plan[1]
    assert [wave["name"] for wave in payments_plan["waves"]] == ["Ring 1 wave", "Ring 2 wave"]
    assert [wave["vm_count"] for wave in payments_plan["waves"]] == [2, 1]
    assert payments_plan["vm_count"] == 3
    assert payments_plan["starts_at"] == NOW + timedelta(hours=1)
    assert payments_plan["waves"][0]["target"] == "Payments / Ring 1"
    # One 60s stagger between the two machines in the first wave.
    assert payments_plan["waves"][0]["finishes_at"] == NOW + timedelta(hours=1, seconds=60)


async def test_rollout_plan_ignores_schedules_without_a_next_run(session) -> None:
    application = await make_application(session)
    ring = await make_ring(session, application, "Ring 1")
    await make_vm(session, ring, "vm-alpha")
    await make_schedule(session, "Unscheduled wave", ring, next_run_at=None)
    disabled_ring = await make_ring(session, application, "Ring 2", sequence=2)
    await make_vm(session, disabled_ring, "vm-beta")
    await make_schedule(session, "Disabled wave", disabled_ring, enabled=False, next_run_at=NOW + timedelta(hours=1))

    assert (await overview(session))["rollout_plan"] == []


# -- power -------------------------------------------------------------


async def test_power_summarises_the_cached_scan_columns(session) -> None:
    application = await make_application(session)
    ring = await make_ring(session, application, "Ring 1")
    newest = NOW - timedelta(minutes=5)
    await make_vm(session, ring, "vm-alpha", power_state="running", power_state_at=NOW - timedelta(hours=1))
    await make_vm(session, ring, "vm-beta", power_state="running", power_state_at=newest)
    await make_vm(session, ring, "vm-gamma", power_state="deallocated", power_state_at=NOW - timedelta(hours=3))
    await make_vm(session, ring, "vm-delta")
    await make_vm(session, ring, "vm-epsilon")

    power = (await overview(session))["power"]

    assert power["counts"] == {"deallocated": 1, "running": 2}
    assert power["never_scanned"] == 2
    assert power["last_scan_at"] == newest


async def test_power_is_empty_when_nothing_has_ever_been_scanned(session) -> None:
    application = await make_application(session)
    await make_vm(session, application, "vm-alpha")

    power = (await overview(session))["power"]

    assert power["counts"] == {}
    assert power["never_scanned"] == 1
    assert power["last_scan_at"] is None


# -- offenders ---------------------------------------------------------


async def test_offenders_rank_vms_by_failure_count_and_cap_at_eight(session) -> None:
    run = await make_run(session, INSIDE_AT, status="failed", total_count=11, succeeded_count=0, failed_count=11)
    await make_attempt(session, run, "vm-a-heavy", "failed", INSIDE_AT + timedelta(minutes=1), message="First failure")
    await make_attempt(session, run, "vm-a-heavy", "timed_out", INSIDE_AT + timedelta(minutes=2), sequence=1, message="Middle failure")
    await make_attempt(session, run, "vm-a-heavy", "failed", INSIDE_AT + timedelta(minutes=3), sequence=2, message="Latest failure")
    for index in range(1, 9):
        await make_attempt(session, run, f"vm-fail-{index}", "failed", INSIDE_AT + timedelta(minutes=4), sequence=index + 2, message=f"Failure {index}")
    await make_attempt(session, run, "vm-healthy", "succeeded", INSIDE_AT + timedelta(minutes=5), sequence=20)

    offenders = (await overview(session))["offenders"]

    assert len(offenders) == 8
    assert offenders[0]["vm_name"] == "vm-a-heavy"
    assert offenders[0]["failures"] == 3
    assert offenders[0]["last_message"] == "Latest failure"
    assert offenders[0]["last_at"] == INSIDE_AT + timedelta(minutes=3)
    assert offenders[0]["run_id"] == run.id
    # The remaining single-failure machines sort by name, so vm-fail-8 falls off the cap.
    assert [item["vm_name"] for item in offenders[1:]] == [f"vm-fail-{index}" for index in range(1, 8)]
    assert "vm-healthy" not in {item["vm_name"] for item in offenders}


async def test_offenders_ignore_failures_outside_the_window(session) -> None:
    outside = await make_run(session, OUTSIDE_AT, status="failed", total_count=1, succeeded_count=0, failed_count=1)
    previous = await make_run(session, PREVIOUS_AT, status="failed", total_count=1, succeeded_count=0, failed_count=1)
    await make_attempt(session, outside, "vm-old", "failed", OUTSIDE_AT + timedelta(minutes=1))
    await make_attempt(session, previous, "vm-previous", "failed", PREVIOUS_AT + timedelta(minutes=1))

    assert (await overview(session))["offenders"] == []


# -- estate ------------------------------------------------------------


async def test_estate_counts_applications_rings_vms_and_schedules(session) -> None:
    application = await make_application(session)
    ring = await make_ring(session, application, "Ring 1")
    await make_vm(session, ring, "vm-alpha")
    await make_vm(session, ring, "vm-beta", enabled=False)
    await make_schedule(session, "Ring 1 wave", ring)
    await make_schedule(session, "Retired wave", application, enabled=False)

    estate = (await overview(session))["estate"]

    assert estate == {
        "application_count": 1,
        "ring_count": 1,
        "vm_count": 2,
        "enabled_vm_count": 1,
        "schedule_count": 2,
        "enabled_schedule_count": 1,
    }


# -- HTTP endpoint -----------------------------------------------------


@pytest.mark.parametrize(
    "to",
    [WINDOW_START.isoformat(), (WINDOW_START - timedelta(hours=1)).isoformat()],
)
async def test_overview_endpoint_rejects_a_window_that_does_not_move_forward(session, to: str) -> None:
    async with api_client(session) as client:
        response = await client.get("/api/overview", params={"from": WINDOW_START.isoformat(), "to": to})
    assert response.status_code == 422


async def test_overview_endpoint_rejects_a_window_longer_than_180_days(session) -> None:
    start = (WINDOW_END - timedelta(days=181)).isoformat()
    async with api_client(session) as client:
        response = await client.get("/api/overview", params={"from": start, "to": WINDOW_END.isoformat()})
    assert response.status_code == 422


async def test_overview_endpoint_returns_the_panels_for_a_valid_window(session, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the request in-process: no on-disk connection registry, no Azure call.
    async def fake_list_connections(public: bool = False) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr("app.main.list_connections", fake_list_connections)
    application = await make_application(session)
    ring = await make_ring(session, application, "Ring 1")
    await make_vm(session, ring, "vm-alpha")
    await make_schedule(session, "Ring 1 wave", ring)
    await make_run(session, INSIDE_AT)

    async with api_client(session) as client:
        response = await client.get("/api/overview", params={"from": WINDOW_START.isoformat(), "to": WINDOW_END.isoformat()})

    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "window",
        "generated_at",
        "estate",
        "kpis",
        "trend",
        "reliability",
        "readiness",
        "coverage",
        "power",
        "applications",
        "rollout_plan",
        "offenders",
    }
    assert body["estate"]["application_count"] == 1
    assert body["kpis"]["runs"]["current"] == 1
    assert len(body["trend"]) == 14
