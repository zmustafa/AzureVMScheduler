from __future__ import annotations

import asyncio
import logging
import random
import socket
from typing import Any
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .azure import AzurePermanentError, AzureTransientError, get_vm_adapter, resolve_action_mode
from .config import get_settings
from .connections import get_connection
from .database import SessionLocal
from . import firewall
from .hierarchy import effective_connection_id, is_stop_protected, load_schedule_index, load_tree, resolve_schedule_vms
from .models import AuditLog, Group, Schedule, ScheduleRun, SecurityPolicy, VirtualMachine, VmAttempt, new_id, utcnow
from .notifications import publish, run_daily_digests
from .recurrence import Recurrence, localize as localize_wall_clock
from .recurrence import next_occurrence as recurrence_next
from .templating import run_url
from .validation import parse_vm_resource_id, resolve_default_timezone, validate_timezone


logger = logging.getLogger(__name__)

PENDING_STATUSES = ("pending", "claimed", "starting")
MONITORING_STATUS = "monitoring"
TERMINAL_STATUSES = {"succeeded", "failed", "timed_out", "skipped", "cancelled"}
ADAPTER_TTL_SECONDS = 1500
RUN_EVENTS = {"succeeded": ("run.succeeded", "info"), "partially_failed": ("run.partially_failed", "warning"), "failed": ("run.failed", "error"), "timed_out": ("run.timed_out", "error")}
# A failed start is an outage; a failed stop is a cost problem. Keeping them apart lets rules
# route them with different urgency.
ATTEMPT_EVENTS = {
    "start": {"failed": ("vm.start_failed", "error"), "timed_out": ("vm.start_timed_out", "error"), "skipped": ("vm.start_skipped", "warning")},
    "stop": {"failed": ("vm.stop_failed", "warning"), "timed_out": ("vm.stop_timed_out", "warning"), "skipped": ("vm.stop_skipped", "warning")},
}


def _localize(value: datetime, zone: ZoneInfo) -> datetime:
    localized = localize_wall_clock(value, zone)
    if localized is None:
        raise ValueError("start_time falls in a daylight-saving time gap")
    return localized


def recurrence_of(schedule: Schedule) -> Recurrence:
    """The calendar rule behind a stored schedule."""
    return Recurrence(
        schedule_type=schedule.schedule_type,
        timezone=schedule.timezone,
        start_time=schedule.start_time or "",
        cron_expression=schedule.cron_expression or "",
        weekday=schedule.weekday,
        start_date=schedule.start_date or "",
        end_date=schedule.end_date or "",
        run_limit=schedule.run_limit,
        run_count=schedule.run_count or 0,
    )


def parse_schedule_time(schedule_type: str, start_time: str, timezone_name: str, now: datetime | None = None) -> datetime:
    """First occurrence of a simple one_time/daily rule. Kept for callers that have no weekday or cron."""
    moment = recurrence_next(Recurrence(schedule_type=schedule_type, timezone=timezone_name, start_time=start_time), now)
    if moment is None:
        raise ValueError("This schedule has no future occurrences")
    return moment


def next_occurrence(schedule: Schedule, after: datetime | None = None) -> datetime | None:
    return recurrence_next(recurrence_of(schedule), after or utcnow())


def roll_up_run_status(statuses: list[str]) -> str:
    if not statuses:
        # A wave that resolved no VMs started nothing, so it must not claim success.
        return "skipped"
    if any(status not in TERMINAL_STATUSES for status in statuses):
        return "running"
    succeeded = sum(status == "succeeded" for status in statuses)
    cancelled = sum(status == "cancelled" for status in statuses)
    failures = [status for status in statuses if status in {"failed", "timed_out"}]
    if not failures and not cancelled:
        return "succeeded"
    if not failures:
        return "cancelled" if succeeded == 0 else "partially_failed"
    if succeeded or cancelled:
        return "partially_failed"
    return "timed_out" if all(status == "timed_out" for status in failures) else "failed"


def roll_up_mode(modes: list[str]) -> str:
    distinct = {mode for mode in modes if mode and mode != "pending"}
    if not distinct:
        return "pending"
    return distinct.pop() if len(distinct) == 1 else "mixed"


async def claim_due_schedules(session: AsyncSession, worker_id: str, limit: int | None = None) -> list[str]:
    now = utcnow()
    settings = get_settings()
    batch = limit or settings.scheduler_claim_batch
    lease_seconds = max(settings.scheduler_lease_seconds, settings.vm_monitor_timeout_seconds + 60)
    lease_until = now + timedelta(seconds=lease_seconds)
    due = await session.scalars(
        select(Schedule.id)
        .where(
            Schedule.enabled.is_(True),
            Schedule.next_run_at.is_not(None),
            Schedule.next_run_at <= now,
            or_(Schedule.lease_until.is_(None), Schedule.lease_until < now),
        )
        .order_by(Schedule.next_run_at)
        .limit(batch)
    )
    claimed: list[str] = []
    schedule_ids = list(due.all())
    if schedule_ids:
        # One conditional UPDATE for the whole batch rather than one per schedule. The lease is
        # still optimistic: only rows that satisfied the guards carry our owner afterwards, so a
        # schedule another worker took in the meantime simply is not returned.
        await session.execute(
            update(Schedule)
            .where(
                Schedule.id.in_(schedule_ids),
                Schedule.enabled.is_(True),
                Schedule.next_run_at.is_not(None),
                Schedule.next_run_at <= now,
                or_(Schedule.lease_until.is_(None), Schedule.lease_until < now),
            )
            .values(lease_owner=worker_id, lease_until=lease_until)
        )
        claimed = list((await session.scalars(
            select(Schedule.id).where(
                Schedule.id.in_(schedule_ids),
                Schedule.lease_owner == worker_id,
                Schedule.lease_until == lease_until,
            )
        )).all())
    await session.commit()
    return claimed


async def create_run(
    session: AsyncSession,
    schedule: Schedule,
    trigger: str = "scheduler",
    triggered_by: str | None = None,
    vm_ids: list[str] | None = None,
) -> ScheduleRun:
    """Fan a schedule out into one attempt per resolved VM."""
    tree = await load_tree(session)
    index = await load_schedule_index(session)
    vms = await resolve_schedule_vms(session, schedule, tree, index)
    if vm_ids is not None:
        allowed = set(vm_ids)
        vms = [vm for vm in vms if vm.id in allowed]
    run = ScheduleRun(
        id=new_id(),
        schedule_id=schedule.id,
        schedule_name=schedule.name,
        action=schedule.action or "start",
        stop_mode=schedule.stop_mode or "deallocate",
        scheduled_for=schedule.next_run_at or utcnow(),
        started_at=utcnow(),
        status="pending",
        mode="pending",
        trigger=trigger,
        triggered_by=triggered_by,
        total_count=len(vms),
    )
    session.add(run)
    for position, vm in enumerate(vms):
        session.add(VmAttempt(
            id=new_id(),
            schedule_id=schedule.id,
            run_id=run.id,
            vm_id=vm.id,
            vm_resource_id=vm.vm_resource_id,
            connection_id=effective_connection_id(tree, vm) or schedule.azure_connection_id,
            action=schedule.action or "start",
            stop_mode=schedule.stop_mode or "deallocate",
            status="pending",
            mode="pending",
            sequence=position,
            correlation_id=new_id(),
        ))
    session.add(AuditLog(actor_id=triggered_by, action="schedule.run_started", target_type="schedule_run", target_id=run.id, detail=f'{{"schedule_id":"{schedule.id}","trigger":"{trigger}","vm_count":{len(vms)}}}'))
    await session.commit()
    return run


async def finalize_run_if_complete(session: AsyncSession, run_id: str) -> ScheduleRun | None:
    run = await session.get(ScheduleRun, run_id)
    if not run or run.finished_at:
        return run
    # Called after every attempt finishes, so a 30-VM wave calls it 30 times. Reading all 30 rows
    # each time made finishing a wave quadratic in its size; the tallies are all this needs.
    tallies = (await session.execute(
        select(VmAttempt.status, VmAttempt.mode, func.count())
        .where(VmAttempt.run_id == run_id)
        .group_by(VmAttempt.status, VmAttempt.mode)
    )).all()
    statuses = [value for value, _mode, count in tallies for _ in range(count)]
    modes = [mode for _value, mode, count in tallies for _ in range(count)]
    run.total_count = len(statuses)
    run.succeeded_count = sum(status == "succeeded" for status in statuses)
    run.failed_count = sum(status in {"failed", "timed_out"} for status in statuses)
    run.skipped_count = sum(status in {"skipped", "cancelled"} for status in statuses)
    run.mode = roll_up_mode(modes)
    status = roll_up_run_status(statuses)
    run.started_at = run.started_at or utcnow()
    if status == "running":
        run.status = "running"
        await session.commit()
        return run
    run.status = status
    run.finished_at = utcnow()
    schedule = await session.get(Schedule, run.schedule_id) if run.schedule_id else None
    if schedule and run.trigger == "scheduler":
        # Only scheduler-triggered runs spend the budget; a manual "run now" is free.
        schedule.run_count = (schedule.run_count or 0) + 1
        if schedule.schedule_type == "one_time":
            schedule.status = "completed" if status == "succeeded" else status
            schedule.next_run_at = None
        else:
            upcoming = next_occurrence(schedule, utcnow()) if schedule.enabled else None
            schedule.next_run_at = upcoming
            if not schedule.enabled:
                schedule.status = "disabled"
            elif upcoming is None:
                # The run limit is spent or the end date has passed: the schedule is simply done.
                schedule.status = "completed"
            else:
                schedule.status = "scheduled"
        schedule.lease_owner = None
        schedule.lease_until = None
    session.add(AuditLog(actor_id=run.triggered_by, action="schedule.run_completed", target_type="schedule_run", target_id=run.id, detail=f'{{"schedule_id":"{run.schedule_id or ""}","status":"{run.status}","total":{run.total_count},"succeeded":{run.succeeded_count},"failed":{run.failed_count},"skipped":{run.skipped_count}}}'))
    await session.commit()
    # The full rows are only needed to describe the wave in an event, so they are read once here
    # rather than on every one of the calls that found the wave still running.
    attempts = list((await session.scalars(select(VmAttempt).where(VmAttempt.run_id == run_id))).all()) if run.status in RUN_EVENTS else []
    await _publish_run_event(session, run, schedule, attempts)
    return run


async def _connection_label(connection_id: str | None) -> str:
    if not connection_id:
        return ""
    try:
        connection = await get_connection(connection_id)
    except Exception:
        return ""
    return str((connection or {}).get("display_name") or (connection or {}).get("tenant_id") or "")


async def _schedule_group_id(session: AsyncSession, schedule: Schedule | None) -> str | None:
    if not schedule:
        return None
    if schedule.target_type == "group":
        return schedule.target_id
    vm = await session.get(VirtualMachine, schedule.target_id)
    return vm.group_id if vm else None


async def _hierarchy_labels(session: AsyncSession, group_id: str | None) -> tuple[str, str]:
    """(application, ring) names for a group.

    Read through the group's own path rather than by loading the tree: this runs once per published
    event, and a wave that fails thirty machines would otherwise read the whole groups table thirty
    times to name the same two rows.
    """
    if not group_id:
        return "", ""
    node = await session.get(Group, group_id)
    if not node:
        return "", ""
    ancestry = [item for item in (node.path or "").split("/") if item]
    if not ancestry:
        return node.name, ""
    found = {item.id: item for item in (await session.scalars(select(Group).where(Group.id.in_(ancestry)))).all()}
    chain = [found[item] for item in ancestry if item in found]
    if not chain:
        return "", ""
    leaf = chain[-1]
    return chain[0].name, (leaf.name if leaf.depth else "")


async def _publish_run_event(session: AsyncSession, run: ScheduleRun, schedule: Schedule | None, attempts: list[VmAttempt]) -> None:
    """One event per wave so a 30-VM ring produces one message, not thirty."""
    mapped = RUN_EVENTS.get(run.status)
    if not mapped:
        return
    event_type, severity = mapped
    failed = [item for item in attempts if item.status in {"failed", "timed_out"}]
    names = dict((await session.execute(select(VirtualMachine.id, VirtualMachine.vm_name).where(VirtualMachine.id.in_([item.vm_id for item in failed if item.vm_id])))).all()) if failed else {}
    group_id = await _schedule_group_id(session, schedule)
    application, ring = await _hierarchy_labels(session, group_id)
    connection_id = next((item.connection_id for item in attempts if item.connection_id), schedule.azure_connection_id if schedule else None)
    facts = {
        "application": application,
        "ring": ring,
        "schedule_name": run.schedule_name,
        "scheduled_for": (run.scheduled_for or run.created_at).replace(tzinfo=(run.scheduled_for or run.created_at).tzinfo or timezone.utc).isoformat(),
        "vm_count": run.total_count,
        "succeeded": run.succeeded_count,
        "failed": run.failed_count,
        "skipped": run.skipped_count,
        "failed_vm_names": [names.get(item.vm_id) or item.vm_resource_id.rsplit("/", 1)[-1] for item in failed],
        "tenant": await _connection_label(connection_id),
        "run_url": run_url(run.id),
        "error": next((item.message for item in failed if item.message), ""),
    }
    title = f"{run.schedule_name or 'Schedule'} {run.status.replace('_', ' ')} — {run.succeeded_count}/{run.total_count} succeeded · {run.failed_count} failed"
    body = f"{application or 'Unassigned'}{f' / {ring}' if ring else ''} wave finished with status {run.status}."
    await publish(session, type=event_type, severity=severity, title=title, body=body, facts=facts, schedule_id=run.schedule_id, run_id=run.id, group_id=group_id, connection_id=connection_id)


async def _publish_attempt_event(session: AsyncSession, attempt: VmAttempt) -> None:
    mapped = ATTEMPT_EVENTS.get(attempt.action or "start", {}).get(attempt.status)
    if not mapped:
        return
    event_type, severity = mapped
    vm = await session.get(VirtualMachine, attempt.vm_id) if attempt.vm_id else None
    schedule = await session.get(Schedule, attempt.schedule_id) if attempt.schedule_id else None
    group_id = vm.group_id if vm else await _schedule_group_id(session, schedule)
    application, ring = await _hierarchy_labels(session, group_id)
    vm_name = vm.vm_name if vm else attempt.vm_resource_id.rsplit("/", 1)[-1]
    facts = {
        "application": application,
        "ring": ring,
        "schedule_name": schedule.name if schedule else "",
        "vm_count": 1,
        "succeeded": 0,
        "failed": 1 if attempt.status != "skipped" else 0,
        "failed_vm_names": [vm_name],
        "tenant": await _connection_label(attempt.connection_id),
        "run_url": run_url(attempt.run_id),
        "error": attempt.message,
    }
    action = attempt.action or "start"
    await publish(session, type=event_type, severity=severity, title=f"{vm_name} {attempt.status.replace('_', ' ')}", body=attempt.message or f"The {action} attempt for {vm_name} ended as {attempt.status}.", facts=facts, schedule_id=attempt.schedule_id, run_id=attempt.run_id, vm_id=attempt.vm_id, group_id=group_id, connection_id=attempt.connection_id)


async def detect_missed_runs(session: AsyncSession, now: datetime | None = None) -> list[str]:
    """Emit schedule.missed exactly once per occurrence; the fingerprint survives an outage."""
    now = now or utcnow()
    policy = await session.get(SecurityPolicy, 1)
    cutoff = now - timedelta(seconds=policy.schedule_missed_grace_seconds if policy else 300)
    overdue = (await session.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run_at.is_not(None), Schedule.next_run_at < cutoff))).all()
    emitted: list[str] = []
    for schedule in overdue:
        occurrence = schedule.next_run_at
        if occurrence is None:
            continue
        if await session.scalar(select(ScheduleRun.id).where(ScheduleRun.schedule_id == schedule.id, ScheduleRun.scheduled_for == occurrence).limit(1)):
            continue
        stamp = occurrence.replace(tzinfo=occurrence.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
        group_id = await _schedule_group_id(session, schedule)
        application, ring = await _hierarchy_labels(session, group_id)
        event = await publish(
            session,
            type="schedule.missed",
            severity="critical",
            title=f"{schedule.name} did not run",
            body=f"The occurrence due at {stamp} has no run and is past the missed-run grace period.",
            facts={"application": application, "ring": ring, "schedule_name": schedule.name, "scheduled_for": stamp, "vm_count": 0, "succeeded": 0, "failed": 0, "tenant": await _connection_label(schedule.azure_connection_id), "error": "No run was created for this occurrence"},
            schedule_id=schedule.id,
            group_id=group_id,
            connection_id=schedule.azure_connection_id,
            fingerprint=f"schedule.missed:{schedule.id}:{stamp}",
        )
        if event:
            emitted.append(event.id)
    return emitted


class SchedulerService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.worker_id = f"{socket.gethostname()}-{id(self)}"
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._start_queue: asyncio.Queue[str] = asyncio.Queue()
        self._monitor_queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscription_slots: dict[str, asyncio.Semaphore] = {}
        self._adapters: dict[str, tuple[Any, dict[str, Any], str, float]] = {}
        self._adapter_lock = asyncio.Lock()

    async def start(self) -> None:
        self._stop.clear()
        for index in range(self.settings.scheduler_start_concurrency):
            self._tasks.append(asyncio.create_task(self._start_worker(), name=f"azureops-start-{index}"))
        for index in range(self.settings.scheduler_monitor_concurrency):
            self._tasks.append(asyncio.create_task(self._monitor_worker(), name=f"azureops-monitor-{index}"))
        self._tasks.append(asyncio.create_task(self._poll_loop(), name="azureops-scheduler"))
        await self._reconcile()

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except Exception:  # shutdown must never raise
                pass
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        await self._close_adapters()

    # -- dispatch -------------------------------------------------------

    def submit_run(self, run_id: str, stagger_seconds: int = 0) -> None:
        self._tasks.append(asyncio.create_task(self._dispatch_run(run_id, stagger_seconds), name=f"azureops-run-{run_id}"))

    async def _dispatch_run(self, run_id: str, stagger_seconds: int) -> None:
        """Feed attempts into the start queue, honouring the per-VM stagger without holding a worker."""
        try:
            async with SessionLocal() as session:
                attempt_ids = list((await session.scalars(
                    select(VmAttempt.id)
                    .where(VmAttempt.run_id == run_id, VmAttempt.status.in_(PENDING_STATUSES))
                    .order_by(VmAttempt.sequence)
                )).all())
                if not attempt_ids:
                    await finalize_run_if_complete(session, run_id)
                    return
            for position, attempt_id in enumerate(attempt_ids):
                if position and stagger_seconds > 0:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=stagger_seconds)
                        return
                    except TimeoutError:
                        pass
                await self._start_queue.put(attempt_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to dispatch run %s", run_id)

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                async with SessionLocal() as session:
                    await detect_missed_runs(session)
                    await run_daily_digests(session)
                    # The IP allowlist keeps its state in memory and its block log out of the
                    # request path; this is where both are reconciled with the database.
                    await firewall.maintain(session)
                    for schedule_id in await claim_due_schedules(session, self.worker_id):
                        schedule = await session.get(Schedule, schedule_id)
                        if not schedule:
                            continue
                        run = await create_run(session, schedule, trigger="scheduler")
                        self.submit_run(run.id, schedule.stagger_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler polling cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.scheduler_poll_seconds)
            except TimeoutError:
                continue

    async def _reconcile(self) -> None:
        """Resume attempts that were mid-flight when the process restarted."""
        try:
            async with SessionLocal() as session:
                pending = list((await session.scalars(select(VmAttempt.id).where(VmAttempt.status.in_(PENDING_STATUSES)).order_by(VmAttempt.sequence))).all())
                monitoring = list((await session.scalars(select(VmAttempt.id).where(VmAttempt.status == MONITORING_STATUS))).all())
                stale_runs = list((await session.scalars(select(ScheduleRun.id).where(ScheduleRun.finished_at.is_(None)))).all())
                for run_id in stale_runs:
                    await finalize_run_if_complete(session, run_id)
                await detect_missed_runs(session)
            for attempt_id in pending:
                await self._start_queue.put(attempt_id)
            for attempt_id in monitoring:
                await self._monitor_queue.put(attempt_id)
        except Exception:
            logger.exception("Scheduler reconciliation failed")

    # -- workers --------------------------------------------------------

    async def _start_worker(self) -> None:
        while not self._stop.is_set():
            attempt_id = await self._start_queue.get()
            try:
                await self._submit_start(attempt_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never strand the attempt: an unhandled error here would leave the run pending forever.
                logger.exception("Start submission failed for attempt %s", attempt_id)
                await self._fail_quietly(attempt_id, f"Internal error submitting the request: {exc}")
            finally:
                self._start_queue.task_done()

    async def _monitor_worker(self) -> None:
        while not self._stop.is_set():
            attempt_id = await self._monitor_queue.get()
            try:
                await self._monitor(attempt_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Power-state monitoring failed for attempt %s", attempt_id)
                await self._fail_quietly(attempt_id, f"Internal error while monitoring: {exc}")
            finally:
                self._monitor_queue.task_done()

    async def _fail_quietly(self, attempt_id: str, message: str) -> None:
        try:
            await self._finish(attempt_id, "failed", message)
        except Exception:
            logger.exception("Could not record the failure for attempt %s", attempt_id)

    async def _submit_start(self, attempt_id: str) -> None:
        async with SessionLocal() as session:
            attempt = await session.get(VmAttempt, attempt_id)
            if not attempt or attempt.status not in PENDING_STATUSES:
                return
            action = attempt.action or "start"
            stop_mode = attempt.stop_mode or "deallocate"
            # Never fight another wave over the same machine: a start and a stop racing each other
            # would leave the VM in whichever state happened to land last.
            if attempt.vm_id:
                conflicting = await session.scalar(
                    select(func.count())
                    .select_from(VmAttempt)
                    .where(
                        VmAttempt.vm_id == attempt.vm_id,
                        VmAttempt.id != attempt.id,
                        VmAttempt.action != action,
                        VmAttempt.status.in_((*PENDING_STATUSES, MONITORING_STATUS)),
                    )
                )
                if conflicting:
                    opposite = "stop" if action == "start" else "start"
                    await session.rollback()
                    await self._finish(attempt_id, "skipped", f"A {opposite} for this virtual machine is still in flight")
                    return
            attempt.status = "starting"
            attempt.started_at = attempt.started_at or utcnow()
            connection_id, resource_id = attempt.connection_id, attempt.vm_resource_id
            await session.commit()
        try:
            adapter, _, mode = await self._adapter(connection_id, action)
        except Exception as exc:
            await self._finish(attempt_id, "skipped", f"Azure connection unavailable: {exc}")
            return
        try:
            subscription = parse_vm_resource_id(resource_id).subscription_id
        except ValueError as exc:
            await self._finish(attempt_id, "failed", str(exc), mode)
            return
        delay = 1.0
        for round_number in range(self.settings.azure_start_max_retries + 1):
            try:
                async with self._subscription_slot(subscription):
                    if action == "stop":
                        await adapter.stop_vm(resource_id, stop_mode)
                    else:
                        await adapter.start_vm(resource_id)
                break
            except AzureTransientError as exc:
                if round_number >= self.settings.azure_start_max_retries:
                    await self._finish(attempt_id, "failed", f"Azure kept throttling the {action} request: {exc}", mode)
                    return
                wait = min(exc.retry_after or delay, 60) + random.uniform(0, 1)
                delay = min(delay * 2, 60)
                await asyncio.sleep(wait)
            except (AzurePermanentError, ValueError) as exc:
                await self._finish(attempt_id, "failed", str(exc), mode)
                return
            except Exception as exc:  # operational failures are persisted, never raised
                await self._finish(attempt_id, "failed", str(exc), mode)
                return
        async with SessionLocal() as session:
            attempt = await session.get(VmAttempt, attempt_id)
            if attempt and attempt.status == "starting":
                # Carried here rather than in a transaction of its own: the resolved mode has
                # nowhere useful to be until the attempt moves on anyway.
                attempt.status = MONITORING_STATUS
                attempt.mode = mode
                await session.commit()
        await self._monitor_queue.put(attempt_id)

    async def _monitor(self, attempt_id: str) -> None:
        async with SessionLocal() as session:
            attempt = await session.get(VmAttempt, attempt_id)
            if not attempt or attempt.status != MONITORING_STATUS:
                return
            connection_id, resource_id = attempt.connection_id, attempt.vm_resource_id
            action = attempt.action or "start"
            stop_mode = attempt.stop_mode or "deallocate"
        try:
            adapter, _, _ = await self._adapter(connection_id, action)
            state = await adapter.wait_until_stopped(resource_id, stop_mode) if action == "stop" else await adapter.wait_until_running(resource_id)
        except TimeoutError as exc:
            await self._finish(attempt_id, "timed_out", str(exc) or f"Timed out waiting for the VM to {action}")
            return
        except Exception as exc:
            await self._finish(attempt_id, "failed", str(exc))
            return
        await self._finish(attempt_id, "succeeded", f"VM reached {state} state")

    async def _finish(self, attempt_id: str, status: str, message: str, mode: str | None = None) -> None:
        async with SessionLocal() as session:
            attempt = await session.get(VmAttempt, attempt_id)
            if not attempt:
                return
            attempt.status = status
            attempt.message = message[:2000]
            if mode:
                attempt.mode = mode
            attempt.completed_at = utcnow()
            await session.commit()
            await _publish_attempt_event(session, attempt)
            if attempt.run_id:
                await finalize_run_if_complete(session, attempt.run_id)

    # -- azure plumbing -------------------------------------------------

    def _subscription_slot(self, subscription_id: str) -> asyncio.Semaphore:
        slot = self._subscription_slots.get(subscription_id)
        if slot is None:
            slot = asyncio.Semaphore(self.settings.azure_subscription_concurrency)
            self._subscription_slots[subscription_id] = slot
        return slot

    async def _adapter(self, connection_id: str | None, action: str = "start") -> tuple[Any, dict[str, Any], str]:
        # The permission decision is re-made on every attempt so that marking a tenant read-only,
        # withdrawing an action, disabling it, or turning off a global gate takes effect at once.
        # Only the expensive part — the ARM credential — is cached, and only while the decision
        # it was built under still holds.
        policy, mode = await resolve_action_mode(connection_id, action)
        key = f"{action}:{connection_id or ''}"
        loop_time = asyncio.get_running_loop().time()
        async with self._adapter_lock:
            cached = self._adapters.get(key)
            if cached and cached[2] == mode and loop_time - cached[3] < ADAPTER_TTL_SECONDS:
                return cached[0], cached[1], cached[2]
            if cached:
                await _close_quietly(cached[0])
            adapter, connection, resolved = await get_vm_adapter(connection_id, action)
            self._adapters[key] = (adapter, connection or policy or {}, resolved, loop_time)
            return adapter, connection or {}, resolved

    async def _close_adapters(self) -> None:
        async with self._adapter_lock:
            for adapter, _, _, _ in self._adapters.values():
                await _close_quietly(adapter)
            self._adapters.clear()


async def _close_quietly(adapter: Any) -> None:
    try:
        await adapter.close()
    except Exception:
        logger.debug("Closing the Azure adapter failed", exc_info=True)


async def trigger_schedule_run(
    schedule_id: str,
    actor_id: str | None,
    service: SchedulerService | None = None,
    vm_ids: list[str] | None = None,
) -> ScheduleRun | None:
    async with SessionLocal() as session:
        schedule = await session.get(Schedule, schedule_id)
        if not schedule:
            return None
        run = await create_run(session, schedule, trigger="manual", triggered_by=actor_id, vm_ids=vm_ids)
        stagger = schedule.stagger_seconds
    if service:
        service.submit_run(run.id, stagger)
    return run


async def trigger_adhoc_run(
    vm_ids: list[str],
    action: str,
    actor_id: str | None,
    service: SchedulerService | None = None,
    stop_mode: str = "deallocate",
    stagger_seconds: int = 0,
) -> ScheduleRun:
    """A wave with no schedule behind it, for acting on a hand-picked set of machines.

    Stop waves still honour never_stop; a protected machine can never be caught by one.
    """
    async with SessionLocal() as session:
        tree = await load_tree(session)
        machines = list((await session.scalars(select(VirtualMachine).where(VirtualMachine.id.in_(vm_ids)))).all())
        if action == "stop":
            machines = [item for item in machines if not is_stop_protected(tree, item)]
        machines.sort(key=lambda item: (item.vm_name.lower(), item.id))

        run = ScheduleRun(
            id=new_id(),
            schedule_id=None,
            schedule_name=f"Manual {action} of {len(machines)} virtual machine{'' if len(machines) == 1 else 's'}",
            action=action,
            stop_mode=stop_mode,
            scheduled_for=utcnow(),
            started_at=utcnow(),
            status="pending",
            mode="pending",
            trigger="manual",
            triggered_by=actor_id,
            total_count=len(machines),
        )
        session.add(run)
        for position, vm in enumerate(machines):
            session.add(VmAttempt(
                id=new_id(),
                schedule_id=None,
                run_id=run.id,
                vm_id=vm.id,
                vm_resource_id=vm.vm_resource_id,
                connection_id=effective_connection_id(tree, vm),
                action=action,
                stop_mode=stop_mode,
                status="pending",
                mode="pending",
                sequence=position,
                correlation_id=new_id(),
            ))
        session.add(AuditLog(
            actor_id=actor_id,
            action=f"vm.manual_{action}",
            target_type="schedule_run",
            target_id=run.id,
            detail=f'{{"vm_count":{len(machines)},"stop_mode":"{stop_mode}"}}',
        ))
        await session.commit()
    if service:
        service.submit_run(run.id, stagger_seconds)
    return run
