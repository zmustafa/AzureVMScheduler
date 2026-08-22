"""Query budgets for the listing endpoints.

These assert a *shape*, not a speed: an N+1 grows with the number of rows, so a budget that a
correct implementation clears comfortably is tripped the moment one comes back. The numbers are
deliberately loose -- they are not a target to optimise against, only a tripwire.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from app.hierarchy import child_path
from app.models import Group, Schedule, SecurityPolicy, VirtualMachine, new_id, utcnow
from test_runs import api_client


APPLICATIONS = 4
RINGS = 3
VMS_PER_RING = 6


@contextmanager
def counted(session) -> Iterator[dict[str, int]]:
    """Count SQL statements issued on the session's engine while the block runs."""
    bind = session.get_bind()
    target = getattr(bind, "sync_engine", bind)
    tally = {"queries": 0}

    def on_execute(*_args, **_kwargs) -> None:
        tally["queries"] += 1

    event.listen(target, "before_cursor_execute", on_execute)
    try:
        yield tally
    finally:
        event.remove(target, "before_cursor_execute", on_execute)


async def seed_estate(session) -> None:
    session.add(SecurityPolicy(id=1))
    for app_index in range(APPLICATIONS):
        application = Group(id=new_id(), parent_id=None, name=f"App {app_index}", depth=0, sequence=app_index)
        application.path = child_path(None, application.id)
        session.add(application)
        for ring_index in range(RINGS):
            ring = Group(id=new_id(), parent_id=application.id, name=f"Ring {ring_index}", depth=1, sequence=ring_index)
            ring.path = child_path(application, ring.id)
            session.add(ring)
            for vm_index in range(VMS_PER_RING):
                name = f"vm-{app_index}-{ring_index}-{vm_index}"
                resource_id = (
                    f"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-{app_index}"
                    f"/providers/Microsoft.Compute/virtualMachines/{name}"
                )
                session.add(VirtualMachine(
                    id=new_id(), group_id=ring.id, vm_resource_id=resource_id,
                    normalized_resource_id=resource_id.lower(), display_name=name, vm_name=name,
                    subscription_id="00000000-0000-0000-0000-000000000001",
                    resource_group=f"rg-{app_index}", enabled=True,
                ))
            for action, hour in (("start", 7), ("stop", 19)):
                session.add(Schedule(
                    id=new_id(), name=f"{application.name} {ring.name} {action}", action=action,
                    schedule_type="daily", start_time=f"{hour:02d}:00", timezone="UTC",
                    target_type="group", target_id=ring.id, enabled=True, status="scheduled",
                    next_run_at=utcnow(),
                ))
    await session.commit()


@pytest.mark.parametrize(
    ("path", "budget"),
    [
        # 72 machines and 24 schedules. Resolving a wave per schedule would put these in the
        # dozens; the bulk resolver keeps them flat.
        ("/api/schedules?limit=200", 12),
        ("/api/timeline", 12),
        ("/api/schedules/upcoming?limit=10", 12),
        ("/api/groups?shape=tree", 10),
        ("/api/vms?limit=200", 10),
        ("/api/dashboard", 14),
    ],
)
async def test_listing_endpoints_stay_within_their_query_budget(session, path: str, budget: int) -> None:
    await seed_estate(session)
    async with api_client(session) as client:
        # Warm anything cached per process so the budget measures the request, not first use.
        await client.get(path)
        with counted(session) as tally:
            response = await client.get(path)
        assert response.status_code == 200
        assert tally["queries"] <= budget, (
            f"{path} issued {tally['queries']} queries, budget {budget}. "
            "A count that grows with the number of rows means an N+1 came back."
        )


async def test_the_budget_would_actually_catch_an_n_plus_one(session) -> None:
    """Guards the guard: prove the counter sees per-row queries rather than always reading zero."""
    await seed_estate(session)
    async with api_client(session) as client:
        await client.get("/api/vms?limit=200")
        with counted(session) as tally:
            for _ in range(3):
                await client.get("/api/vms?limit=1")
    assert tally["queries"] >= 3
