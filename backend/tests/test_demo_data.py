"""The demo estate must be removable without ever touching a real application."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.demo import DEMO_APPS, demo_status, load_demo_estate, remove_demo_estate
from app.hierarchy import MAX_DEPTH, child_path
from app.models import Group, Schedule, ScheduleRun, VirtualMachine, VmAttempt, new_id

from test_runs import api_client

pytestmark = pytest.mark.asyncio


async def test_loading_builds_the_whole_zava_estate(session) -> None:
    counts = await load_demo_estate(session)
    await session.commit()

    assert counts["applications"] == len(DEMO_APPS)
    assert counts["rings"] == sum(len(app.rings) for app in DEMO_APPS)
    assert counts["virtual_machines"] == sum(len(ring.vms) for app in DEMO_APPS for ring in app.rings)
    assert counts["schedules"] == sum(len(app.waves) for app in DEMO_APPS)

    status = await demo_status(session)
    assert status["loaded"] is True
    assert status["virtual_machines"] == counts["virtual_machines"]


async def test_the_estate_it_builds_obeys_the_two_level_rule(session) -> None:
    await load_demo_estate(session)
    await session.commit()

    groups = (await session.scalars(select(Group))).all()
    assert max(group.depth for group in groups) <= MAX_DEPTH
    by_id = {group.id: group for group in groups}
    for group in groups:
        # A materialised path is what every resolution walk relies on.
        expected = child_path(by_id.get(group.parent_id) if group.parent_id else None, group.id)
        assert group.path == expected
        assert group.is_demo is True


async def test_vms_get_a_parseable_resource_id_and_a_ring(session) -> None:
    await load_demo_estate(session)
    await session.commit()

    vms = (await session.scalars(select(VirtualMachine))).all()
    rings = {group.id for group in (await session.scalars(select(Group).where(Group.depth == 1))).all()}
    for vm in vms:
        assert vm.group_id in rings, "every sample VM belongs to a ring, never straight to an application"
        assert vm.vm_resource_id.startswith("/subscriptions/")
        assert vm.normalized_resource_id == vm.vm_resource_id.lower()
        assert vm.subscription_id and vm.resource_group and vm.vm_name


async def test_every_wave_is_schedulable(session) -> None:
    await load_demo_estate(session)
    await session.commit()

    schedules = (await session.scalars(select(Schedule))).all()
    assert schedules
    for schedule in schedules:
        assert schedule.next_run_at is not None, f"{schedule.name} would never fire"
        assert schedule.target_type == "group"
        assert schedule.status == "scheduled"
    # Stops unwind in reverse so the canary ring is the last one down.
    assert all(item.ring_order == "reverse" for item in schedules if item.action == "stop")


async def test_loading_twice_does_not_duplicate(session) -> None:
    first = await load_demo_estate(session)
    await session.commit()
    second = await load_demo_estate(session)
    await session.commit()

    assert second == {"applications": 0, "rings": 0, "virtual_machines": 0, "schedules": 0}
    status = await demo_status(session)
    assert status["applications"] == first["applications"]


async def test_removal_takes_the_estate_and_its_run_history(session) -> None:
    await load_demo_estate(session)
    await session.commit()

    schedule = (await session.scalars(select(Schedule))).first()
    vm = (await session.scalars(select(VirtualMachine))).first()
    run = ScheduleRun(id=new_id(), schedule_id=schedule.id, action="start", status="succeeded", mode="mock", trigger="manual")
    session.add(run)
    await session.flush()
    session.add(VmAttempt(id=new_id(), run_id=run.id, vm_id=vm.id, vm_resource_id=vm.vm_resource_id, action="start", status="succeeded", mode="mock"))
    await session.commit()

    counts = await remove_demo_estate(session)
    await session.commit()

    assert counts["applications"] == len(DEMO_APPS)
    assert counts["runs"] == 1
    for model in (Group, VirtualMachine, Schedule, ScheduleRun, VmAttempt):
        assert (await session.scalars(select(model))).all() == []
    assert (await demo_status(session))["loaded"] is False


async def test_removal_leaves_real_applications_alone(session) -> None:
    """The whole point of the flag: a real app called Zava must survive."""
    real = Group(id=new_id(), parent_id=None, name="Zava Commerce", depth=0, sequence=0, is_demo=False)
    real.path = child_path(None, real.id)
    session.add(real)
    await session.flush()
    ring = Group(id=new_id(), parent_id=real.id, name="Production", depth=1, sequence=0, is_demo=False)
    ring.path = child_path(real, ring.id)
    session.add(ring)
    await session.flush()
    resource_id = "/subscriptions/8f3b1c62-4a5d-4e7f-9b21-1c7d5e0a4b33/resourceGroups/rg-real/providers/Microsoft.Compute/virtualMachines/vm-real-01"
    session.add(VirtualMachine(id=new_id(), group_id=ring.id, vm_resource_id=resource_id, normalized_resource_id=resource_id.lower(), vm_name="vm-real-01"))
    await session.commit()

    # The name collides, so the loader skips that application but still builds the others.
    loaded = await load_demo_estate(session)
    await session.commit()
    assert loaded["applications"] == len(DEMO_APPS) - 1

    await remove_demo_estate(session)
    await session.commit()

    survivors = (await session.scalars(select(Group))).all()
    assert {group.name for group in survivors} == {"Zava Commerce", "Production"}
    assert len((await session.scalars(select(VirtualMachine))).all()) == 1


async def test_removing_when_nothing_is_loaded_is_a_no_op(session) -> None:
    counts = await remove_demo_estate(session)
    await session.commit()
    assert counts == {"applications": 0, "rings": 0, "virtual_machines": 0, "schedules": 0, "runs": 0}


# -- API -----------------------------------------------------------------


async def test_the_endpoints_round_trip(session) -> None:
    async with api_client(session) as client:
        assert (await client.get("/api/admin/demo-data")).json()["loaded"] is False

        loaded = await client.post("/api/admin/demo-data")
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["status"]["loaded"] is True

        removed = await client.delete("/api/admin/demo-data")
        assert removed.status_code == 200, removed.text
        assert removed.json()["status"]["loaded"] is False
        assert removed.json()["applications"] == len(DEMO_APPS)
