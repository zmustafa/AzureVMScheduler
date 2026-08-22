"""Endpoint profiler: wall time and SQL round trips per request, on a seeded estate.

Not a test — run it directly, before and after a change, to prove an optimization actually
moved the numbers rather than only looking better:

    cd backend
    ..\.venv\Scripts\python.exe -m bench.profile_endpoints
    ..\.venv\Scripts\python.exe -m bench.profile_endpoints --apps 40 --rings 5 --vms-per-ring 20

Query count is the number that matters most here: the app runs on a single SQLite replica, so an
endpoint issuing one query per row does not scale no matter how fast each query is.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from datetime import timedelta

import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  (registers the mappings)
from app.auth import current_user, require_csrf
from app.database import Base, get_db
from app.hierarchy import child_path
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
    utcnow,
)

ADMIN_ID = "00000000-0000-0000-0000-0000000000ad"

#: (label, method, path). Every one of these is polled by the UI or hit on navigation.
ENDPOINTS: list[tuple[str, str, str]] = [
    ("GET /api/dashboard", "GET", "/api/dashboard"),
    ("GET /api/overview", "GET", "/api/overview"),
    ("GET /api/groups?shape=tree", "GET", "/api/groups?shape=tree"),
    ("GET /api/vms (200)", "GET", "/api/vms?limit=200"),
    ("GET /api/schedules (200)", "GET", "/api/schedules?limit=200"),
    ("GET /api/schedules/upcoming", "GET", "/api/schedules/upcoming?limit=10"),
    ("GET /api/timeline", "GET", "/api/timeline"),
    ("GET /api/runs (50)", "GET", "/api/runs?limit=50"),
    ("GET /api/runs/activity", "GET", "/api/runs/activity"),
    ("GET /api/auth/me", "GET", "/api/auth/me"),
]


class QueryCounter:
    """Counts statements on the engine, so an N+1 shows up as a number rather than a suspicion."""

    def __init__(self, engine) -> None:
        self.count = 0
        self._armed = False

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _on_execute(*_args, **_kwargs) -> None:
            if self._armed:
                self.count += 1

    def start(self) -> None:
        self.count = 0
        self._armed = True

    def stop(self) -> int:
        self._armed = False
        return self.count


async def seed(session, *, apps: int, rings: int, vms_per_ring: int, runs: int) -> dict[str, int]:
    """Build an estate shaped like a real one: applications, rings beneath them, VMs in rings."""
    session.add(SecurityPolicy(id=1))
    session.add(User(id=ADMIN_ID, username="bench", role="admin", must_change_password=False))

    machines: list[VirtualMachine] = []
    schedules: list[Schedule] = []

    for app_index in range(apps):
        application = Group(id=new_id(), parent_id=None, name=f"App {app_index:03d}", depth=0, sequence=app_index)
        application.path = child_path(None, application.id)
        session.add(application)

        for ring_index in range(rings):
            ring = Group(id=new_id(), parent_id=application.id, name=f"Ring {ring_index}", depth=1, sequence=ring_index)
            ring.path = child_path(application, ring.id)
            session.add(ring)

            for vm_index in range(vms_per_ring):
                name = f"vm-{app_index:03d}-{ring_index}-{vm_index:02d}"
                resource_id = (
                    f"/subscriptions/00000000-0000-0000-0000-00000000000{app_index % 10}"
                    f"/resourceGroups/rg-{app_index:03d}/providers/Microsoft.Compute/virtualMachines/{name}"
                )
                machines.append(VirtualMachine(
                    id=new_id(), group_id=ring.id, vm_resource_id=resource_id,
                    normalized_resource_id=resource_id.lower(), display_name=name, vm_name=name,
                    subscription_id=f"00000000-0000-0000-0000-00000000000{app_index % 10}",
                    resource_group=f"rg-{app_index:03d}", enabled=True,
                    last_power_state="running" if vm_index % 3 else "deallocated",
                    last_power_state_at=utcnow(),
                ))

            # One start wave and one stop wave per ring, which is the shape the product encourages.
            for action, hour in (("start", 7), ("stop", 19)):
                schedules.append(Schedule(
                    id=new_id(), name=f"App {app_index:03d} / Ring {ring_index} {action}",
                    action=action, schedule_type="daily", start_time=f"{hour:02d}:{ring_index * 5:02d}",
                    timezone="America/New_York", target_type="group", target_id=ring.id,
                    stagger_seconds=30, enabled=True, status="scheduled",
                    next_run_at=utcnow() + timedelta(hours=hour),
                    ring_order="reverse" if action == "stop" else "sequence",
                ))

    session.add_all(machines)
    session.add_all(schedules)

    for run_index in range(runs):
        run = ScheduleRun(
            id=new_id(), schedule_id=schedules[run_index % len(schedules)].id,
            schedule_name=schedules[run_index % len(schedules)].name,
            action="start", scheduled_for=utcnow() - timedelta(hours=run_index),
            started_at=utcnow() - timedelta(hours=run_index),
            finished_at=utcnow() - timedelta(hours=run_index) + timedelta(minutes=4),
            status="succeeded" if run_index % 5 else "partially_failed",
            mode="mock", trigger="scheduler", total_count=5, succeeded_count=5,
            created_at=utcnow() - timedelta(hours=run_index),
        )
        session.add(run)
        for position in range(5):
            session.add(VmAttempt(
                id=new_id(), schedule_id=run.schedule_id, run_id=run.id,
                vm_id=machines[(run_index * 5 + position) % len(machines)].id,
                vm_resource_id=machines[(run_index * 5 + position) % len(machines)].vm_resource_id,
                action="start", status="succeeded", mode="mock", sequence=position,
                claimed_at=utcnow() - timedelta(hours=run_index),
                started_at=utcnow() - timedelta(hours=run_index),
                completed_at=utcnow() - timedelta(hours=run_index) + timedelta(minutes=2),
            ))

    await session.commit()
    return {"applications": apps, "rings": apps * rings, "vms": len(machines), "schedules": len(schedules), "runs": runs}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apps", type=int, default=20)
    parser.add_argument("--rings", type=int, default=5)
    parser.add_argument("--vms-per-ring", type=int, default=10)
    parser.add_argument("--runs", type=int, default=60)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    counter = QueryCounter(engine)

    async with maker() as session:
        shape = await seed(session, apps=args.apps, rings=args.rings, vms_per_ring=args.vms_per_ring, runs=args.runs)
        admin = await session.get(User, ADMIN_ID)

        app.dependency_overrides[get_db] = lambda: session
        app.dependency_overrides[current_user] = lambda: admin
        app.dependency_overrides[require_csrf] = lambda: admin
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
                print(f"\nestate: {shape}")
                print(f"{'endpoint':<30}{'status':>8}{'queries':>10}{'median ms':>12}{'max ms':>10}")
                print("-" * 70)
                results = []
                for label, method, path in ENDPOINTS:
                    timings: list[float] = []
                    queries = 0
                    status = 0
                    for attempt in range(args.repeat):
                        counter.start()
                        began = time.perf_counter()
                        response = await client.request(method, path)
                        elapsed = (time.perf_counter() - began) * 1000
                        seen = counter.stop()
                        status = response.status_code
                        if attempt:  # discard the first pass, which pays import and cache warm-up
                            timings.append(elapsed)
                            queries = seen
                    median = statistics.median(timings) if timings else 0.0
                    worst = max(timings) if timings else 0.0
                    results.append((label, status, queries, median, worst))
                    flag = "" if status < 400 else "  <-- FAILED"
                    print(f"{label:<30}{status:>8}{queries:>10}{median:>12.1f}{worst:>10.1f}{flag}")
                print("-" * 70)
                print(f"{'TOTAL':<30}{'':>8}{sum(r[2] for r in results):>10}{sum(r[3] for r in results):>12.1f}\n")
        finally:
            app.dependency_overrides.clear()

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
