from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.hierarchy import (
    MAX_GROUP_NAME,
    assert_move_allowed,
    assert_parent_allowed,
    assert_unique_sibling_name,
    effective_connection_id,
    effective_schedule,
    ensure_group_path,
    load_schedule_index,
    load_tree,
    next_sequence,
    path_ids,
    recompute_subtree,
    resolve_schedule_vms,
)
from app.models import Group, Schedule, VirtualMachine, VmAttempt, new_id
from app.scheduling import create_run, finalize_run_if_complete
from test_runs import api_client


VM_TEMPLATE = "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/{name}"


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


async def make_schedule(session, target_type: str, target_id: str, name: str = "wave", **kwargs) -> Schedule:
    schedule = Schedule(id=new_id(), name=name, schedule_type="daily", start_time="07:00", timezone="America/New_York", target_type=target_type, target_id=target_id, **kwargs)
    session.add(schedule)
    await session.commit()
    return schedule


async def test_path_and_depth_are_materialized(session) -> None:
    application = await make_group(session, "Payments")
    ring = await make_group(session, "Ring 1", application)

    assert application.path == f"/{application.id}/"
    assert ring.path == f"/{application.id}/{ring.id}/"
    assert [application.depth, ring.depth] == [0, 1]
    assert application.kind == "application"
    assert ring.kind == "ring"
    assert path_ids(ring.path) == [application.id, ring.id]


async def test_move_recomputes_the_whole_subtree(session) -> None:
    first = await make_group(session, "Payments")
    second = await make_group(session, "Ledger")
    ring = await make_group(session, "Ring 1", first)

    ring.parent_id = second.id
    await recompute_subtree(session, ring)
    await session.commit()

    assert ring.path == f"/{second.id}/{ring.id}/"
    assert ring.depth == 1


async def test_cycles_are_prevented(session) -> None:
    application = await make_group(session, "Payments")
    ring = await make_group(session, "Ring 1", application)
    tree = await load_tree(session)

    with pytest.raises(ValueError, match="into itself"):
        assert_move_allowed(tree, application.id, application.id)
    with pytest.raises(ValueError, match="own subtree"):
        assert_move_allowed(tree, application.id, ring.id)
    assert_move_allowed(tree, ring.id, None)


async def test_a_ring_cannot_gain_a_child(session) -> None:
    application = await make_group(session, "Payments")
    ring = await make_group(session, "Ring 1", application)

    assert_parent_allowed(None)
    assert_parent_allowed(application)
    with pytest.raises(ValueError, match="ring cannot contain another ring"):
        assert_parent_allowed(ring)


async def test_moves_cannot_nest_rings_or_demote_a_populated_application(session) -> None:
    payments = await make_group(session, "Payments")
    ledger = await make_group(session, "Ledger")
    ring = await make_group(session, "Ring 1", payments)
    other_ring = await make_group(session, "Ring 1", ledger)
    tree = await load_tree(session)

    with pytest.raises(ValueError, match="ring cannot contain another ring"):
        assert_move_allowed(tree, ring.id, other_ring.id)
    with pytest.raises(ValueError, match="Move the rings out"):
        assert_move_allowed(tree, payments.id, ledger.id)
    # Promoting a ring back to a top-level application is always allowed.
    assert_move_allowed(tree, ring.id, None)
    # And a ring may move to another application.
    assert_move_allowed(tree, ring.id, ledger.id)


async def test_ensure_group_path_stops_at_two_segments(session) -> None:
    with pytest.raises(ValueError, match="ring cannot contain another ring"):
        await ensure_group_path(session, ["Payments", "Ring 1", "Ring 1a"])

    ring = await ensure_group_path(session, ["Payments", "Ring 1"])
    await session.commit()

    assert ring.depth == 1
    assert ring.name == "Ring 1"
    application = await session.get(Group, ring.parent_id)
    assert application is not None and application.depth == 0 and application.name == "Payments"


async def test_ensure_group_path_rejects_over_length_names(session) -> None:
    with pytest.raises(ValueError, match="Group names cannot exceed 200 characters"):
        await ensure_group_path(session, ["A" * (MAX_GROUP_NAME + 1)])
    with pytest.raises(ValueError, match="Group names cannot exceed 200 characters"):
        await ensure_group_path(session, ["Payments", "R" * (MAX_GROUP_NAME + 1)])

    ring = await ensure_group_path(session, ["A" * MAX_GROUP_NAME, "R" * MAX_GROUP_NAME])
    await session.commit()

    assert ring.name == "R" * MAX_GROUP_NAME
    application = await session.get(Group, ring.parent_id)
    assert application is not None and application.name == "A" * MAX_GROUP_NAME


async def test_group_api_persists_stop_protection_on_create_and_update(session) -> None:
    """The stop-safety flag must not be accepted by the schema and then silently discarded."""
    async with api_client(session) as client:
        created = await client.post("/api/groups", json={"name": "Protected", "never_stop": True})
        assert created.status_code == 201, created.text
        assert created.json()["never_stop"] is True

        updated = await client.patch(f"/api/groups/{created.json()['id']}", json={"never_stop": False})
        assert updated.status_code == 200, updated.text
        assert updated.json()["never_stop"] is False

    stored = await session.get(Group, created.json()["id"])
    assert stored is not None and stored.never_stop is False


async def test_group_api_rejects_an_unknown_connection_override(session, monkeypatch) -> None:
    monkeypatch.setattr("app.main.list_connections", AsyncMock(return_value=[]))
    async with api_client(session) as client:
        response = await client.post("/api/groups", json={"name": "Invalid", "azure_connection_id": "missing"})

    assert response.status_code == 422
    assert "does not exist" in response.text


async def test_a_referenced_connection_cannot_be_deleted(session, monkeypatch) -> None:
    connection_id = "connection-in-use"
    await make_group(session, "Payments", azure_connection_id=connection_id)
    remove = AsyncMock(return_value=True)
    monkeypatch.setattr("app.main.list_connections", AsyncMock(return_value=[{"id": connection_id}]))
    monkeypatch.setattr("app.main.delete_connection", remove)

    async with api_client(session) as client:
        response = await client.delete(f"/api/connections/{connection_id}")

    assert response.status_code == 409
    assert "1 group(s)" in response.text
    remove.assert_not_awaited()


async def test_sibling_names_are_unique_case_insensitively(session) -> None:
    application = await make_group(session, "Payments")
    await make_group(session, "Ring 1", application)
    await make_group(session, "Ring 1")  # a different parent may reuse the name

    with pytest.raises(ValueError, match="already exists"):
        await assert_unique_sibling_name(session, application.id, "ring 1")


async def test_nearest_schedule_shadows_ancestors(session) -> None:
    application = await make_group(session, "Payments")
    ring = await make_group(session, "Ring 1", application)
    vm = await make_vm(session, ring, "vm-a")
    outer = await make_schedule(session, "group", application.id, "outer")
    inner = await make_schedule(session, "group", ring.id, "inner")

    tree, index = await load_tree(session), await load_schedule_index(session)
    assert effective_schedule(index, tree, vm).id == inner.id

    inner.enabled = False
    await session.commit()
    index = await load_schedule_index(session)
    assert effective_schedule(index, tree, vm).id == outer.id


async def test_vm_binding_beats_every_group_schedule(session) -> None:
    application = await make_group(session, "Payments")
    vm = await make_vm(session, application, "vm-a")
    await make_schedule(session, "group", application.id, "group wave")
    direct = await make_schedule(session, "vm", vm.id, "vm override")

    tree, index = await load_tree(session), await load_schedule_index(session)
    assert effective_schedule(index, tree, vm).id == direct.id


async def test_effective_connection_walks_up_to_the_nearest_override(session) -> None:
    application = await make_group(session, "Payments", azure_connection_id="conn-app")
    ring = await make_group(session, "Ring 1", application)
    vm = await make_vm(session, ring, "vm-a")

    tree = await load_tree(session)
    assert effective_connection_id(tree, vm) == "conn-app"

    ring.azure_connection_id = "conn-ring"
    await session.commit()
    tree = await load_tree(session)
    assert effective_connection_id(tree, vm) == "conn-ring"

    vm.azure_connection_id = "conn-vm"
    await session.commit()
    tree = await load_tree(session)
    assert effective_connection_id(tree, vm) == "conn-vm"


async def test_fan_out_excludes_vms_owned_by_a_nearer_schedule(session) -> None:
    application = await make_group(session, "Payments")
    ring = await make_group(session, "Ring 1", application)
    outer_vm = await make_vm(session, application, "vm-outer")
    inner_vm = await make_vm(session, ring, "vm-inner")
    overridden_vm = await make_vm(session, application, "vm-override")
    disabled_vm = await make_vm(session, application, "vm-disabled", enabled=False)

    outer = await make_schedule(session, "group", application.id, "outer")
    await make_schedule(session, "group", ring.id, "inner")
    await make_schedule(session, "vm", overridden_vm.id, "direct")

    resolved = await resolve_schedule_vms(session, outer)
    names = {item.vm_name for item in resolved}
    assert names == {outer_vm.vm_name}
    assert inner_vm.vm_name not in names
    assert overridden_vm.vm_name not in names
    assert disabled_vm.vm_name not in names


async def test_fan_out_skips_subtrees_below_a_disabled_group(session) -> None:
    application = await make_group(session, "Payments")
    ring = await make_group(session, "Ring 1", application, enabled=False)
    await make_vm(session, ring, "vm-inner")
    await make_vm(session, application, "vm-outer")
    schedule = await make_schedule(session, "group", application.id)

    resolved = await resolve_schedule_vms(session, schedule)
    assert [item.vm_name for item in resolved] == ["vm-outer"]


async def test_fan_out_orders_by_sibling_sequence_then_name(session) -> None:
    application = await make_group(session, "Payments")
    first_ring = await make_group(session, "Ring 1", application)
    second_ring = await make_group(session, "Ring 2", application)
    await make_vm(session, second_ring, "vm-z")
    await make_vm(session, first_ring, "vm-b")
    await make_vm(session, first_ring, "vm-a")
    schedule = await make_schedule(session, "group", application.id)

    resolved = await resolve_schedule_vms(session, schedule)
    assert [item.vm_name for item in resolved] == ["vm-a", "vm-b", "vm-z"]


async def test_create_run_snapshots_every_vm_in_stagger_order(session) -> None:
    application = await make_group(session, "Payments", azure_connection_id="conn-app")
    ring = await make_group(session, "Ring 1", application)
    await make_vm(session, ring, "vm-b")
    await make_vm(session, ring, "vm-a", azure_connection_id="conn-vm")
    schedule = await make_schedule(session, "group", application.id, stagger_seconds=30)

    run = await create_run(session, schedule, trigger="manual", triggered_by=None)
    attempts = (await session.scalars(select(VmAttempt).where(VmAttempt.run_id == run.id).order_by(VmAttempt.sequence))).all()

    assert run.total_count == 2
    assert [item.sequence for item in attempts] == [0, 1]
    assert [item.connection_id for item in attempts] == ["conn-vm", "conn-app"]
    assert all(item.vm_resource_id for item in attempts)


async def test_a_new_run_starts_its_clock_and_claims_no_mode_yet(session) -> None:
    """Duration is measured from wave creation, and nothing may report "mock" before it has run."""
    application = await make_group(session, "Payments")
    await make_vm(session, application, "vm-a")
    schedule = await make_schedule(session, "group", application.id)

    run = await create_run(session, schedule, trigger="scheduler")
    attempts = (await session.scalars(select(VmAttempt).where(VmAttempt.run_id == run.id))).all()

    assert run.started_at is not None
    assert run.mode == "pending"
    assert [item.mode for item in attempts] == ["pending"]


async def test_a_wave_that_resolves_no_vms_is_skipped_rather_than_succeeded(session) -> None:
    application = await make_group(session, "Payments")
    schedule = await make_schedule(session, "group", application.id)

    run = await create_run(session, schedule, trigger="scheduler")
    finished = await finalize_run_if_complete(session, run.id)

    assert run.total_count == 0
    assert finished.status == "skipped"
    assert (finished.succeeded_count, finished.failed_count) == (0, 0)


async def test_run_finalization_rolls_up_counts_and_reschedules(session) -> None:
    application = await make_group(session, "Payments")
    await make_vm(session, application, "vm-a")
    await make_vm(session, application, "vm-b")
    schedule = await make_schedule(session, "group", application.id)
    run = await create_run(session, schedule, trigger="scheduler")

    attempts = (await session.scalars(select(VmAttempt).where(VmAttempt.run_id == run.id).order_by(VmAttempt.sequence))).all()
    assert await finalize_run_if_complete(session, run.id) and run.status == "running"

    attempts[0].status, attempts[1].status = "succeeded", "failed"
    await session.commit()
    finished = await finalize_run_if_complete(session, run.id)

    assert finished.status == "partially_failed"
    assert (finished.succeeded_count, finished.failed_count) == (1, 1)
    assert finished.finished_at is not None
    assert schedule.next_run_at is not None and schedule.lease_until is None
