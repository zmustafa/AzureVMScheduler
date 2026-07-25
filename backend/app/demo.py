"""Sample Zava estate, so a fresh install has something to look at.

Demo applications are flagged with `groups.is_demo`. Everything else is reached through the tree —
rings are the children of a demo application, VMs belong to those groups, and a schedule is demo
when it targets one of them. That means removal deletes exactly what this module created and can
never touch a real application, even one that happens to be called Zava.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .hierarchy import child_path
from .models import Group, Schedule, ScheduleRun, VirtualMachine, VmAttempt, new_id
from .recurrence import Recurrence, next_occurrence
from .validation import normalize_resource_id, parse_vm_resource_id


@dataclass(frozen=True)
class _Ring:
    name: str
    description: str
    vms: list[str]
    never_stop: bool = False


@dataclass(frozen=True)
class _App:
    name: str
    description: str
    resource_group: str
    subscription: str
    rings: list[_Ring]
    #: (action, HH:MM, weekday-or-None, stagger seconds)
    waves: list[tuple[str, str, int | None, int]] = field(default_factory=list)


SUB_COMMERCE = "8f3b1c62-4a5d-4e7f-9b21-1c7d5e0a4b33"
SUB_PLATFORM = "2a9e7d10-6b34-4f18-8c55-9d0e3a6f72b1"
SUB_DATA = "c14d8b95-3e27-4a60-b8d3-57f2e91c4a08"
"""Well-formed GUIDs that resolve to nothing in Azure, so the estate behaves like a real one while
every attempt against it stays in mock mode."""

DEMO_APPS: list[_App] = [
    _App(
        name="Zava Commerce",
        description="Public storefront and checkout. Ships weekly behind a canary ring.",
        resource_group="rg-zava-commerce",
        subscription=SUB_COMMERCE,
        rings=[
            _Ring("Canary", "First ring to start, last to stop.", ["vm-commerce-canary-01"]),
            _Ring("Pilot", "Internal users and a slice of live traffic.", ["vm-commerce-pilot-01", "vm-commerce-pilot-02"]),
            _Ring("Production", "Full customer traffic.", ["vm-commerce-prod-01", "vm-commerce-prod-02", "vm-commerce-prod-03", "vm-commerce-prod-04"]),
        ],
        waves=[("start", "06:30", None, 60), ("stop", "20:00", None, 60)],
    ),
    _App(
        name="Zava Payments",
        description="Card capture and settlement. Production never stops.",
        resource_group="rg-zava-payments",
        subscription=SUB_COMMERCE,
        rings=[
            _Ring("Canary", "Synthetic transactions only.", ["vm-payments-canary-01"]),
            _Ring("Production", "Settlement runs overnight, so these stay up.", ["vm-payments-prod-01", "vm-payments-prod-02"], never_stop=True),
        ],
        waves=[("start", "05:45", None, 120)],
    ),
    _App(
        name="Zava Analytics",
        description="Nightly batch and the interactive query tier.",
        resource_group="rg-zava-analytics",
        subscription=SUB_DATA,
        rings=[
            _Ring("Batch", "Spun up for the nightly load, then torn down.", ["vm-analytics-batch-01", "vm-analytics-batch-02", "vm-analytics-batch-03"]),
            _Ring("Interactive", "Analyst-facing, business hours only.", ["vm-analytics-bi-01", "vm-analytics-bi-02"]),
        ],
        waves=[("start", "22:00", None, 30), ("stop", "04:30", None, 30)],
    ),
    _App(
        name="Zava Intranet",
        description="Employee portal. Weekday hours, weekends off.",
        resource_group="rg-zava-intranet",
        subscription=SUB_PLATFORM,
        rings=[
            _Ring("Pilot", "IT department dogfooding.", ["vm-intranet-pilot-01"]),
            _Ring("Production", "Everyone else.", ["vm-intranet-prod-01", "vm-intranet-prod-02"]),
        ],
        waves=[("start", "07:00", 0, 45), ("stop", "19:00", 4, 45)],
    ),
]

DEMO_TIMEZONE = "America/New_York"


def _resource_id(subscription: str, resource_group: str, vm_name: str) -> str:
    return f"/subscriptions/{subscription}/resourceGroups/{resource_group}/providers/Microsoft.Compute/virtualMachines/{vm_name}"


async def demo_status(db: AsyncSession) -> dict[str, int | bool]:
    """Counts for the Settings card, so the button can say what it is about to do."""
    app_ids = (await db.scalars(select(Group.id).where(Group.is_demo.is_(True), Group.depth == 0))).all()
    if not app_ids:
        return {"loaded": False, "applications": 0, "rings": 0, "virtual_machines": 0, "schedules": 0}
    group_ids = await _demo_group_ids(db)
    vm_ids = (await db.scalars(select(VirtualMachine.id).where(VirtualMachine.group_id.in_(group_ids)))).all()
    schedules = await _demo_schedule_ids(db, group_ids, vm_ids)
    return {
        "loaded": True,
        "applications": len(app_ids),
        "rings": len(group_ids) - len(app_ids),
        "virtual_machines": len(vm_ids),
        "schedules": len(schedules),
    }


async def _demo_group_ids(db: AsyncSession) -> list[str]:
    """Demo applications and their rings. Depth is capped at 1, so one hop covers the tree."""
    app_ids = list((await db.scalars(select(Group.id).where(Group.is_demo.is_(True), Group.depth == 0))).all())
    if not app_ids:
        return []
    ring_ids = list((await db.scalars(select(Group.id).where(Group.parent_id.in_(app_ids)))).all())
    return app_ids + ring_ids


async def _demo_schedule_ids(db: AsyncSession, group_ids: list[str], vm_ids: list[str]) -> list[str]:
    if not group_ids and not vm_ids:
        return []
    matches = ((Schedule.target_type == "group") & Schedule.target_id.in_(group_ids)) | (
        (Schedule.target_type == "vm") & Schedule.target_id.in_(vm_ids)
    )
    return list((await db.scalars(select(Schedule.id).where(matches))).all())


async def load_demo_estate(db: AsyncSession, created_by: str | None = None) -> dict[str, int]:
    """Create the sample estate. Caller commits. Idempotent: an app already present is left alone."""
    existing = {
        name.casefold()
        for name in (await db.scalars(select(Group.name).where(Group.parent_id.is_(None)))).all()
    }
    counts = {"applications": 0, "rings": 0, "virtual_machines": 0, "schedules": 0}
    sequence = int(await db.scalar(select(func.coalesce(func.max(Group.sequence), -1)).where(Group.parent_id.is_(None))) or -1)

    for spec in DEMO_APPS:
        if spec.name.casefold() in existing:
            continue
        sequence += 1
        app = Group(
            id=new_id(), parent_id=None, name=spec.name, description=spec.description,
            depth=0, sequence=sequence, enabled=True, is_demo=True, created_by=created_by,
        )
        app.path = child_path(None, app.id)
        db.add(app)
        counts["applications"] += 1

        for index, ring_spec in enumerate(spec.rings):
            ring = Group(
                id=new_id(), parent_id=app.id, name=ring_spec.name, description=ring_spec.description,
                depth=1, sequence=index, enabled=True, never_stop=ring_spec.never_stop,
                is_demo=True, created_by=created_by,
            )
            ring.path = child_path(app, ring.id)
            db.add(ring)
            counts["rings"] += 1

            for vm_name in ring_spec.vms:
                resource_id = _resource_id(spec.subscription, spec.resource_group, vm_name)
                parsed = parse_vm_resource_id(resource_id)
                db.add(VirtualMachine(
                    id=new_id(), group_id=ring.id, vm_resource_id=resource_id,
                    normalized_resource_id=normalize_resource_id(resource_id), display_name=vm_name,
                    subscription_id=parsed.subscription_id, resource_group=parsed.resource_group,
                    vm_name=parsed.vm_name, enabled=True, created_by=created_by,
                    notes="Sample data. This machine does not exist in Azure.",
                ))
                counts["virtual_machines"] += 1

        for action, start_time, weekday, stagger in spec.waves:
            counts["schedules"] += 1
            db.add(_wave(spec, app.id, action, start_time, weekday, stagger))

    await db.flush()
    return counts


def _wave(spec: _App, group_id: str, action: str, start_time: str, weekday: int | None, stagger: int) -> Schedule:
    schedule_type = "weekly" if weekday is not None else "daily"
    recurrence = Recurrence(schedule_type=schedule_type, timezone=DEMO_TIMEZONE, start_time=start_time, weekday=weekday)
    label = "Start" if action == "start" else "Stop"
    return Schedule(
        id=new_id(),
        name=f"{spec.name} — {label.lower()} wave",
        action=action,
        stop_mode="deallocate",
        # Stops unwind in reverse so the canary ring is the last one down, matching the real default.
        ring_order="reverse" if action == "stop" else "sequence",
        schedule_type=schedule_type,
        start_time=start_time,
        weekday=weekday,
        timezone=DEMO_TIMEZONE,
        target_type="group",
        target_id=group_id,
        stagger_seconds=stagger,
        enabled=True,
        notes="Sample data.",
        status="scheduled",
        next_run_at=next_occurrence(recurrence),
    )


async def remove_demo_estate(db: AsyncSession) -> dict[str, int]:
    """Delete the sample estate and its run history. Caller commits."""
    group_ids = await _demo_group_ids(db)
    if not group_ids:
        return {"applications": 0, "rings": 0, "virtual_machines": 0, "schedules": 0, "runs": 0}

    app_count = int(await db.scalar(select(func.count()).select_from(Group).where(Group.id.in_(group_ids), Group.depth == 0)) or 0)
    vm_ids = list((await db.scalars(select(VirtualMachine.id).where(VirtualMachine.group_id.in_(group_ids)))).all())
    schedule_ids = await _demo_schedule_ids(db, group_ids, vm_ids)
    run_ids = list((await db.scalars(select(ScheduleRun.id).where(ScheduleRun.schedule_id.in_(schedule_ids)))).all()) if schedule_ids else []

    counts = {
        "applications": app_count,
        "rings": len(group_ids) - app_count,
        "virtual_machines": len(vm_ids),
        "schedules": len(schedule_ids),
        "runs": len(run_ids),
    }
    # Explicit ordering: the test engine and some deployments do not enforce ON DELETE cascades.
    if run_ids:
        await db.execute(delete(VmAttempt).where(VmAttempt.run_id.in_(run_ids)))
        await db.execute(delete(ScheduleRun).where(ScheduleRun.id.in_(run_ids)))
    if vm_ids:
        await db.execute(delete(VmAttempt).where(VmAttempt.vm_id.in_(vm_ids)))
    if schedule_ids:
        await db.execute(delete(Schedule).where(Schedule.id.in_(schedule_ids)))
    if vm_ids:
        await db.execute(delete(VirtualMachine).where(VirtualMachine.id.in_(vm_ids)))
    # Rings first: a self-referencing FK means an application cannot go while a child points at it.
    await db.execute(delete(Group).where(Group.id.in_(group_ids), Group.depth > 0))
    await db.execute(delete(Group).where(Group.id.in_(group_ids)))
    return counts
