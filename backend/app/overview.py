"""Operations overview: windowed metrics, readiness checks and coverage gaps.

Everything here is read-only and deliberately built from a small number of grouped queries —
the app runs on a single SQLite replica, so per-application loops would not scale.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .hierarchy import GroupTree, ScheduleIndex, effective_schedule, is_stop_protected, load_schedule_index, load_tree
from .models import (
    Group,
    NotificationDelivery,
    Schedule,
    ScheduleRun,
    SecurityPolicy,
    VirtualMachine,
    VmAttempt,
)
from .scheduling import utcnow

FAILED_RUN_STATUSES = ("failed", "partially_failed", "timed_out")
FAILED_ATTEMPT_STATUSES = ("failed", "timed_out")
TREND_BUCKETS = 14
# A run still unfinished long after the monitor could have given up is almost certainly orphaned.
STUCK_RUN_MULTIPLIER = 2


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=value.tzinfo or timezone.utc) if value else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def _delta(current: int, previous: int) -> dict[str, Any]:
    return {"current": current, "previous": previous, "change": current - previous}


async def _runs_between(db: AsyncSession, start: datetime, end: datetime) -> list[ScheduleRun]:
    return list((await db.scalars(
        select(ScheduleRun).where(ScheduleRun.created_at >= start, ScheduleRun.created_at <= end)
    )).all())


def _trend(runs: list[ScheduleRun], start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Bucketed run outcomes across the window, for the KPI sparklines."""
    span = max((end - start).total_seconds(), 1)
    size = span / TREND_BUCKETS
    buckets = [
        {"start": start + timedelta(seconds=size * index), "runs": 0, "succeeded": 0, "failed": 0, "vms": 0}
        for index in range(TREND_BUCKETS)
    ]
    for run in runs:
        created = _aware(run.created_at)
        if not created:
            continue
        offset = (created - start).total_seconds()
        bucket = buckets[min(int(offset / size), TREND_BUCKETS - 1)] if size else buckets[-1]
        bucket["runs"] += 1
        bucket["vms"] += run.total_count
        if run.status in FAILED_RUN_STATUSES:
            bucket["failed"] += 1
        elif run.status == "succeeded":
            bucket["succeeded"] += 1
    return buckets


def _reliability(runs: list[ScheduleRun], attempts: list[VmAttempt]) -> dict[str, Any]:
    finished = [run for run in runs if run.finished_at]
    succeeded = sum(run.status == "succeeded" for run in finished)
    total_vms = sum(run.total_count for run in finished)
    started_vms = sum(run.succeeded_count for run in finished)

    durations = [
        ((_aware(attempt.completed_at) or _aware(attempt.claimed_at)) - (_aware(attempt.started_at) or _aware(attempt.claimed_at))).total_seconds()
        for attempt in attempts
        if attempt.status == "succeeded" and attempt.completed_at and (attempt.started_at or attempt.claimed_at)
    ]
    lateness = [
        ((_aware(run.started_at) - _aware(run.scheduled_for)).total_seconds())
        for run in runs
        if run.started_at and run.scheduled_for
    ]
    return {
        "runs_finished": len(finished),
        "run_success_rate": round(succeeded / len(finished), 4) if finished else None,
        "vm_success_rate": round(started_vms / total_vms, 4) if total_vms else None,
        "median_seconds_to_running": round(statistics.median(durations), 1) if durations else None,
        "p95_seconds_to_running": round(_percentile(durations, 0.95) or 0, 1) if durations else None,
        "median_lateness_seconds": round(statistics.median(lateness), 1) if lateness else None,
        "worst_lateness_seconds": round(max(lateness), 1) if lateness else None,
    }


def _readiness(
    *,
    real_starts_enabled: bool,
    real_stops_enabled: bool,
    connections: list[dict[str, Any]],
    schedules: list[Schedule],
    tree: GroupTree,
    index: ScheduleIndex,
    machines: list[VirtualMachine],
    schedule_vm_counts: dict[str, int],
    stuck_runs: list[ScheduleRun],
    failed_deliveries: int,
) -> list[dict[str, Any]]:
    """Pre-flight problems that would make the next wave silently do nothing."""
    checks: list[dict[str, Any]] = []
    now = utcnow()

    has_stop_schedules = any(item.enabled and item.action == "stop" for item in schedules)
    if not real_starts_enabled:
        checks.append({
            "id": "mock_mode",
            "severity": "warning",
            "title": "Mock mode: no virtual machine will actually start",
            "detail": "ENABLE_REAL_AZURE_STARTS is off, so every start wave records a simulated result.",
            "link": "/settings",
        })
    if has_stop_schedules and not real_stops_enabled:
        checks.append({
            "id": "mock_mode_stop",
            "severity": "warning",
            "title": "Mock mode: no virtual machine will actually stop",
            "detail": "ENABLE_REAL_AZURE_STOPS is off, so every stop wave records a simulated result.",
            "link": "/settings",
        })

    # Which tenants are actually depended on by an enabled schedule, for either action?
    used_connections: set[str] = set()
    stop_connections: set[str] = set()
    for machine in machines:
        if not machine.enabled:
            continue
        resolved = machine.azure_connection_id or next(
            (node.azure_connection_id for node in tree.chain(machine.group_id) if node.azure_connection_id), None
        )
        if not resolved:
            continue
        if effective_schedule(index, tree, machine, "start"):
            used_connections.add(resolved)
        if effective_schedule(index, tree, machine, "stop") and not is_stop_protected(tree, machine):
            used_connections.add(resolved)
            stop_connections.add(resolved)

    for connection in connections:
        in_use = connection["id"] in used_connections
        name = connection.get("display_name") or "Azure tenant"
        if connection.get("disabled") and in_use:
            checks.append({
                "id": f"tenant_disabled:{connection['id']}",
                "severity": "error",
                "title": f"{name} is disabled but schedules depend on it",
                "detail": "Every virtual machine resolving to this tenant will fail to start.",
                "link": "/settings/tenants",
            })
        if in_use and connection.get("read_only"):
            checks.append({
                "id": f"tenant_read_only:{connection['id']}",
                "severity": "error",
                "title": f"{name} is read-only but schedules depend on it",
                "detail": "Starts and stops against a read-only tenant are refused before Azure is contacted.",
                "link": "/settings/tenants",
            })
        elif in_use and not connection.get("allow_vm_start") and real_starts_enabled:
            checks.append({
                "id": f"tenant_blocked:{connection['id']}",
                "severity": "warning",
                "title": f"{name} does not allow VM starts",
                "detail": "Turn on Allow VM starts for this tenant, or its waves will be refused.",
                "link": "/settings/tenants",
            })
        if connection["id"] in stop_connections and not connection.get("allow_vm_stop") and real_stops_enabled and not connection.get("read_only"):
            checks.append({
                "id": f"tenant_stop_blocked:{connection['id']}",
                "severity": "warning",
                "title": f"{name} does not allow VM stops",
                "detail": "Turn on Allow VM stops for this tenant, or its stop waves will be refused.",
                "link": "/settings/tenants",
            })
        # `token_expires_at` only describes a pasted CLI token, which really does die after an
        # hour. A service principal's secret has its own lifetime that Azure never tells us about,
        # so applying this to every auth method reports a healthy tenant as expired forever.
        expires = connection.get("token_expires_at") if connection.get("auth_method") == "az_cli_token" else None
        if expires:
            try:
                expiry = datetime.fromisoformat(str(expires))
            except ValueError:
                expiry = None
            if expiry:
                expiry = _aware(expiry)
                remaining = (expiry - now).total_seconds()
                if remaining <= 0:
                    checks.append({
                        "id": f"token_expired:{connection['id']}",
                        "severity": "error",
                        "title": f"{name} credentials have expired",
                        "detail": "Paste a fresh token; discovery, power scans, starts and stops will all fail until you do.",
                        "link": "/settings/tenants",
                    })
                elif remaining < 3600:
                    checks.append({
                        "id": f"token_expiring:{connection['id']}",
                        "severity": "warning",
                        "title": f"{name} credentials expire in {int(remaining // 60)} minutes",
                        "detail": "Refresh the token before the next wave needs it.",
                        "link": "/settings/tenants",
                    })

    for schedule in schedules:
        if not schedule.enabled:
            continue
        if schedule_vm_counts.get(schedule.id, 0) == 0:
            checks.append({
                "id": f"empty_schedule:{schedule.id}",
                "severity": "warning",
                "title": f"“{schedule.name}” {'stops' if schedule.action == 'stop' else 'starts'} nothing",
                "detail": "It resolves to zero virtual machines — usually another schedule already owns them.",
                "link": f"/schedules/{schedule.id}",
            })
        elif schedule.target_type == "group":
            node = tree.get(schedule.target_id)
            if node and not tree.is_active(node.id):
                checks.append({
                    "id": f"disabled_target:{schedule.id}",
                    "severity": "warning",
                    "title": f"“{schedule.name}” targets a disabled group",
                    "detail": f"{tree.name_path(node.id)} is disabled, so the wave will skip every machine.",
                    "link": f"/schedules/{schedule.id}",
                })

    checks.extend(_overlap_checks(schedules, tree, index, machines, schedule_vm_counts))

    if stuck_runs:
        checks.append({
            "id": "stuck_runs",
            "severity": "warning",
            "title": f"{len(stuck_runs)} run{'' if len(stuck_runs) == 1 else 's'} never finished",
            "detail": "They are still counted as in flight. Review them and retry the failed machines.",
            "link": "/runs?status=running",
        })

    if failed_deliveries:
        checks.append({
            "id": "failed_deliveries",
            "severity": "warning",
            "title": f"{failed_deliveries} notification{'' if failed_deliveries == 1 else 's'} could not be delivered",
            "detail": "Alerts are not reaching their destination, so failures may go unnoticed.",
            "link": "/notifications/deliveries",
        })

    # Public ingress with nothing filtering it means every password-guessing bot on the internet
    # gets to try. Surfaced here because this is the page an operator actually looks at.
    from . import firewall

    if not firewall.snapshot().active:
        checks.append({
            "id": "no_ip_allowlist",
            "severity": "info",
            "title": "Anyone on the internet can reach the sign-in page",
            "detail": "No IP access control is in force. Restrict which addresses may reach the app to remove brute-force exposure entirely.",
            "link": "/settings/access?tab=firewall",
        })

    return checks


def _wave_window(schedule: Schedule, vm_count: int) -> tuple[int, int] | None:
    """Daily wave as minutes-since-midnight (start, end), including its stagger tail."""
    if schedule.schedule_type != "daily":
        return None
    try:
        hours, _, rest = schedule.start_time.partition(":")
        minutes = int(rest.split(":")[0]) if rest else 0
        begin = int(hours) * 60 + minutes
    except (ValueError, AttributeError):
        return None
    tail = (schedule.stagger_seconds * max(vm_count - 1, 0)) // 60
    return begin, begin + tail


def _overlap_checks(
    schedules: list[Schedule],
    tree: GroupTree,
    index: ScheduleIndex,
    machines: list[VirtualMachine],
    schedule_vm_counts: dict[str, int],
) -> list[dict[str, Any]]:
    """A stop landing inside a start window would kill the wave it collides with."""
    checks: list[dict[str, Any]] = []
    enabled = [item for item in schedules if item.enabled]
    starts = [item for item in enabled if item.action != "stop"]
    stops = [item for item in enabled if item.action == "stop"]
    if not starts or not stops:
        return checks

    # Only compare schedules that actually share machines, and only within the same timezone —
    # comparing wall-clock times across zones would produce false alarms.
    owners: dict[str, set[str]] = {}
    for machine in machines:
        for action in ("start", "stop"):
            owner = effective_schedule(index, tree, machine, action)
            if owner:
                owners.setdefault(owner.id, set()).add(machine.id)

    for stop in stops:
        stop_window = _wave_window(stop, schedule_vm_counts.get(stop.id, 0))
        if not stop_window:
            continue
        for start in starts:
            if start.timezone != stop.timezone:
                continue
            shared = owners.get(start.id, set()) & owners.get(stop.id, set())
            if not shared:
                continue
            start_window = _wave_window(start, schedule_vm_counts.get(start.id, 0))
            if not start_window:
                continue
            if stop_window[0] <= start_window[1] and start_window[0] <= stop_window[1]:
                checks.append({
                    "id": f"overlap:{start.id}:{stop.id}",
                    "severity": "error",
                    "title": f"“{stop.name}” stops machines while “{start.name}” is still starting them",
                    "detail": f"Both waves run at {start.timezone} {start.start_time} and share {len(shared)} virtual machine(s). Move one of them.",
                    "link": f"/schedules/{stop.id}",
                })
    return checks


async def build_overview(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    *,
    connections: list[dict[str, Any]],
    real_starts_enabled: bool,
    real_stops_enabled: bool = False,
    policy: SecurityPolicy,
    monitor_timeout_seconds: int,
) -> dict[str, Any]:
    now = utcnow()
    span = end - start
    previous_start = start - span

    tree = await load_tree(db)
    index = await load_schedule_index(db)
    machines = list((await db.scalars(select(VirtualMachine))).all())
    schedules = list((await db.scalars(select(Schedule))).all())
    runs = await _runs_between(db, start, end)
    previous_runs = await _runs_between(db, previous_start, start)
    run_ids = [run.id for run in runs]
    attempts = list((await db.scalars(select(VmAttempt).where(VmAttempt.run_id.in_(run_ids)))).all()) if run_ids else []

    # -- estate ------------------------------------------------------
    applications = [node for node in tree.by_id.values() if node.depth == 0]
    enabled_vms = [item for item in machines if item.enabled]

    # -- schedule resolution (one pass per action, reused by several panels) -----
    schedule_vm_counts: dict[str, int] = defaultdict(int)
    uncovered: list[VirtualMachine] = []
    starts_but_never_stops: list[VirtualMachine] = []
    stops_but_never_starts: list[VirtualMachine] = []
    protected_count = 0
    for machine in machines:
        start_owner = effective_schedule(index, tree, machine, "start")
        stop_owner = effective_schedule(index, tree, machine, "stop")
        protected = is_stop_protected(tree, machine)
        if protected:
            protected_count += 1
        if start_owner is None and stop_owner is None:
            uncovered.append(machine)
        elif start_owner is not None and stop_owner is None and not protected:
            # Runs around the clock: the machine is started but nothing ever brings it down.
            starts_but_never_stops.append(machine)
        elif stop_owner is not None and start_owner is None:
            # Gets stopped but never started again — an outage waiting to happen.
            stops_but_never_starts.append(machine)
        if machine.enabled and tree.is_active(machine.group_id):
            for owner, action in ((start_owner, "start"), (stop_owner, "stop")):
                if owner and not (action == "stop" and protected):
                    schedule_vm_counts[owner.id] += 1

    # -- windowed counters -------------------------------------------
    def failed_runs(items: list[ScheduleRun]) -> int:
        return sum(item.status in FAILED_RUN_STATUSES for item in items)

    previous_ids = [run.id for run in previous_runs]
    previous_failed_attempts = int(await db.scalar(
        select(func.count()).select_from(VmAttempt).where(
            VmAttempt.run_id.in_(previous_ids), VmAttempt.status.in_(FAILED_ATTEMPT_STATUSES)
        )
    ) or 0) if previous_ids else 0
    failed_attempts = [item for item in attempts if item.status in FAILED_ATTEMPT_STATUSES]

    late_start_count = int(await db.scalar(
        select(func.count()).select_from(Schedule).where(
            Schedule.enabled.is_(True),
            Schedule.next_run_at.is_not(None),
            Schedule.next_run_at < now - timedelta(seconds=policy.schedule_missed_grace_seconds),
        )
    ) or 0)

    stuck_cutoff = now - timedelta(seconds=monitor_timeout_seconds * STUCK_RUN_MULTIPLIER)
    stuck_runs = list((await db.scalars(
        select(ScheduleRun).where(ScheduleRun.finished_at.is_(None), ScheduleRun.created_at < stuck_cutoff)
    )).all())
    running_runs = int(await db.scalar(
        select(func.count()).select_from(ScheduleRun).where(ScheduleRun.finished_at.is_(None))
    ) or 0)

    failed_deliveries = int(await db.scalar(
        select(func.count()).select_from(NotificationDelivery).where(NotificationDelivery.status == "failed")
    ) or 0)

    # -- power summary from the cached scan ---------------------------
    power_counts: dict[str, int] = defaultdict(int)
    last_scan: datetime | None = None
    for machine in machines:
        if machine.last_power_state:
            power_counts[machine.last_power_state] += 1
            stamp = _aware(machine.last_power_state_at)
            if stamp and (last_scan is None or stamp > last_scan):
                last_scan = stamp
    never_scanned = sum(1 for item in machines if not item.last_power_state)

    # -- per-application health ---------------------------------------
    schedules_by_id = {item.id: item for item in schedules}
    app_for_schedule: dict[str, str] = {}
    for schedule in schedules:
        node = tree.get(schedule.target_id) if schedule.target_type == "group" else None
        if node is None and schedule.target_type == "vm":
            machine = next((item for item in machines if item.id == schedule.target_id), None)
            node = tree.get(machine.group_id) if machine else None
        if node:
            chain = list(reversed(tree.chain(node.id)))
            if chain:
                app_for_schedule[schedule.id] = chain[0].id

    runs_by_app: dict[str, list[ScheduleRun]] = defaultdict(list)
    for run in sorted(runs, key=lambda item: _aware(item.created_at) or now, reverse=True):
        app_id = app_for_schedule.get(run.schedule_id or "")
        if app_id:
            runs_by_app[app_id].append(run)

    vms_by_app: dict[str, list[VirtualMachine]] = defaultdict(list)
    for machine in machines:
        chain = list(reversed(tree.chain(machine.group_id)))
        if chain:
            vms_by_app[chain[0].id].append(machine)

    health = []
    for application in sorted(applications, key=lambda item: (item.sequence, item.name.lower())):
        app_runs = runs_by_app.get(application.id, [])
        members = vms_by_app.get(application.id, [])
        covered = sum(1 for item in members if effective_schedule(index, tree, item, "start") or effective_schedule(index, tree, item, "stop"))
        health.append({
            "id": application.id,
            "name": application.name,
            "enabled": tree.is_active(application.id),
            "vm_count": len(members),
            "covered_vm_count": covered,
            "ring_count": len([node for node in tree.by_id.values() if node.parent_id == application.id]),
            "recent": [
                {"run_id": item.id, "status": item.status, "at": _aware(item.created_at), "succeeded": item.succeeded_count, "failed": item.failed_count, "total": item.total_count}
                for item in app_runs[:7]
            ],
            "failed_runs": failed_runs(app_runs),
            "total_runs": len(app_runs),
        })

    # -- rollout plan: what runs next, application by application ------
    plan = []
    for application in sorted(applications, key=lambda item: (item.sequence, item.name.lower())):
        waves = []
        for schedule in schedules:
            if not schedule.enabled or not schedule.next_run_at:
                continue
            if app_for_schedule.get(schedule.id) != application.id:
                continue
            node = tree.get(schedule.target_id) if schedule.target_type == "group" else None
            count = schedule_vm_counts.get(schedule.id, 0)
            waves.append({
                "schedule_id": schedule.id,
                "name": schedule.name,
                "action": schedule.action or "start",
                "stop_mode": schedule.stop_mode or "deallocate",
                "target": tree.name_path(node.id) if node else schedule.name,
                "sequence": node.sequence if node else 0,
                "next_run_at": _aware(schedule.next_run_at),
                "timezone": schedule.timezone,
                "vm_count": count,
                "stagger_seconds": schedule.stagger_seconds,
                "finishes_at": _aware(schedule.next_run_at) + timedelta(seconds=schedule.stagger_seconds * max(count - 1, 0)) if schedule.next_run_at else None,
            })
        if waves:
            waves.sort(key=lambda item: (item["next_run_at"] or now, item["sequence"]))
            plan.append({
                "id": application.id,
                "name": application.name,
                "waves": waves,
                "vm_count": sum(item["vm_count"] for item in waves),
                "starts_at": waves[0]["next_run_at"],
                "finishes_at": max((item["finishes_at"] for item in waves if item["finishes_at"]), default=None),
            })
    plan.sort(key=lambda item: item["starts_at"] or now)

    # -- recurring offenders -------------------------------------------
    by_vm: dict[str, dict[str, Any]] = {}
    for attempt in failed_attempts:
        key = attempt.vm_id or attempt.vm_resource_id
        entry = by_vm.setdefault(key, {
            "vm_id": attempt.vm_id,
            "vm_name": attempt.vm_resource_id.rsplit("/", 1)[-1] or "virtual machine",
            "group_path": "",
            "failures": 0,
            "last_message": "",
            "last_at": None,
            "run_id": None,
        })
        entry["failures"] += 1
        stamp = _aware(attempt.completed_at) or _aware(attempt.claimed_at)
        if stamp and (entry["last_at"] is None or stamp > entry["last_at"]):
            entry["last_at"] = stamp
            entry["last_message"] = attempt.message
            entry["run_id"] = attempt.run_id
    machines_by_id = {item.id: item for item in machines}
    for entry in by_vm.values():
        machine = machines_by_id.get(entry["vm_id"] or "")
        if machine:
            entry["group_path"] = tree.name_path(machine.group_id)
    offenders = sorted(by_vm.values(), key=lambda item: (-item["failures"], item["vm_name"]))[:8]

    # -- coverage gaps ---------------------------------------------------
    disabled_in_scheduled_ring = [
        item for item in machines
        if not item.enabled and (effective_schedule(index, tree, item, "start") or effective_schedule(index, tree, item, "stop"))
    ]
    apps_without_schedules = [
        {"id": application.id, "name": application.name, "vm_count": len(vms_by_app.get(application.id, []))}
        for application in applications
        if not any(app_for_schedule.get(schedule.id) == application.id for schedule in schedules if schedule.enabled)
    ]

    return {
        "window": {"from": start, "to": end, "previous_from": previous_start},
        "generated_at": now,
        "estate": {
            "application_count": len(applications),
            "ring_count": len([node for node in tree.by_id.values() if node.depth > 0]),
            "vm_count": len(machines),
            "enabled_vm_count": len(enabled_vms),
            "schedule_count": len(schedules),
            "enabled_schedule_count": sum(item.enabled for item in schedules),
        },
        "kpis": {
            "runs": _delta(len(runs), len(previous_runs)),
            "failed_runs": _delta(failed_runs(runs), failed_runs(previous_runs)),
            "failed_attempts": _delta(len(failed_attempts), previous_failed_attempts),
            "vms_started": _delta(sum(item.succeeded_count for item in runs), sum(item.succeeded_count for item in previous_runs)),
            "running_runs": running_runs,
            "late_starts": late_start_count,
        },
        "trend": _trend(runs, start, end),
        "reliability": _reliability(runs, attempts),
        "readiness": _readiness(
            real_starts_enabled=real_starts_enabled,
            real_stops_enabled=real_stops_enabled,
            connections=connections,
            schedules=schedules,
            tree=tree,
            index=index,
            machines=machines,
            schedule_vm_counts=schedule_vm_counts,
            stuck_runs=stuck_runs,
            failed_deliveries=failed_deliveries,
        ),
        "coverage": {
            "uncovered_vm_count": len(uncovered),
            "uncovered_sample": [
                {"id": item.id, "vm_name": item.vm_name, "group_path": tree.name_path(item.group_id)}
                for item in uncovered[:8]
            ],
            "disabled_in_scheduled_ring": len(disabled_in_scheduled_ring),
            "applications_without_schedules": apps_without_schedules,
            "empty_schedules": [
                {"id": item.id, "name": item.name, "action": item.action}
                for item in schedules
                if item.enabled and schedule_vm_counts.get(item.id, 0) == 0
            ],
            "starts_but_never_stops": len(starts_but_never_stops),
            "starts_but_never_stops_sample": [
                {"id": item.id, "vm_name": item.vm_name, "group_path": tree.name_path(item.group_id)}
                for item in starts_but_never_stops[:8]
            ],
            "stops_but_never_starts": len(stops_but_never_starts),
            "stops_but_never_starts_sample": [
                {"id": item.id, "vm_name": item.vm_name, "group_path": tree.name_path(item.group_id)}
                for item in stops_but_never_starts[:8]
            ],
            "stop_protected": protected_count,
        },
        "power": {
            "counts": dict(sorted(power_counts.items())),
            "never_scanned": never_scanned,
            "last_scan_at": last_scan,
        },
        "applications": health,
        "rollout_plan": plan,
        "offenders": offenders,
        "schedules_by_id": {key: schedules_by_id[key].name for key in schedules_by_id},
    }
