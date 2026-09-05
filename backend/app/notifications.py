"""Event engine: publish an event, match it against routing rules, and queue deliveries."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .connectors.base import SEVERITY_RANK, severity_of
from .connectors.registry import list_connectors
from .database import SessionLocal
from .delivery import delivery_service
from .hierarchy import GroupTree, load_tree
from .models import NotificationDelivery, NotificationEvent, NotificationEventRead, NotificationRule, new_id, utcnow


logger = logging.getLogger(__name__)

EVENT_TYPES = (
    "run.succeeded", "run.partially_failed", "run.failed", "run.timed_out",
    "vm.start_failed", "vm.start_timed_out", "vm.start_skipped",
    "vm.stop_failed", "vm.stop_timed_out", "vm.stop_skipped",
    "schedule.missed", "connection.unhealthy",
)
PER_VM_EVENTS = {
    "vm.start_failed", "vm.start_timed_out", "vm.start_skipped",
    "vm.stop_failed", "vm.stop_timed_out", "vm.stop_skipped",
}
DIGEST_MODES = ("immediate", "per_vm", "daily")
DIGEST_EVENT = "digest.daily"


def _zone(name: str, fallback: str = "UTC") -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(fallback)


def _parse_hhmm(value: str) -> time | None:
    try:
        return time.fromisoformat((value or "").strip())
    except ValueError:
        return None


def severity_allows(rule: NotificationRule, severity: str) -> bool:
    return SEVERITY_RANK[severity_of(severity)] >= SEVERITY_RANK[severity_of(rule.min_severity or "info")]


def scope_allows(rule: NotificationRule, group_id: str | None, tree: GroupTree | None) -> bool:
    if not rule.scope_group_id:
        return True
    if not group_id:
        return False
    if group_id == rule.scope_group_id:
        return True
    if not rule.include_subtree or tree is None:
        return False
    return group_id in tree.subtree_ids(rule.scope_group_id)


def matches(rule: NotificationRule, event_type: str, severity: str, group_id: str | None = None, tree: GroupTree | None = None) -> bool:
    if not rule.enabled:
        return False
    if not severity_allows(rule, severity):
        return False
    if rule.event_types and event_type not in rule.event_types:
        return False
    return scope_allows(rule, group_id, tree)


def delivers_immediately(rule: NotificationRule, event_type: str) -> bool:
    """`immediate` keeps per-VM noise out of the channel; the run event carries the summary."""
    if rule.digest_mode == "daily":
        return False
    if event_type in PER_VM_EVENTS:
        return rule.digest_mode == "per_vm"
    return True


def in_quiet_hours(rule: NotificationRule, severity: str, now: datetime | None = None) -> bool:
    start, end = _parse_hhmm(rule.quiet_hours_start), _parse_hhmm(rule.quiet_hours_end)
    if start is None or end is None or start == end:
        return False
    if severity_of(severity) == "critical" and rule.critical_ignores_quiet_hours:
        return False
    local = (now or utcnow()).astimezone(_zone(rule.quiet_hours_timezone or "UTC")).time()
    return start <= local < end if start < end else (local >= start or local < end)


async def _throttled(db: AsyncSession, rule: NotificationRule, event_type: str, now: datetime) -> bool:
    if not rule.throttle_minutes or not rule.connector_ids:
        return False
    since = now - timedelta(minutes=rule.throttle_minutes)
    statement = (
        select(NotificationDelivery.id)
        .join(NotificationEvent, NotificationEvent.id == NotificationDelivery.event_id)
        .where(
            NotificationDelivery.connector_id.in_(list(rule.connector_ids)),
            NotificationDelivery.status.in_(["sent", "pending"]),
            NotificationDelivery.created_at >= since,
            NotificationEvent.type == event_type,
        )
        .limit(1)
    )
    return bool(await db.scalar(statement))


async def _connector_labels() -> dict[str, dict[str, Any]]:
    try:
        return {item["id"]: item for item in await list_connectors(public=True)}
    except Exception:
        logger.exception("Could not read the connector registry")
        return {}


def _queue_deliveries(db: AsyncSession, event: NotificationEvent, rule: NotificationRule, labels: dict[str, dict[str, Any]], status: str, detail: str) -> list[str]:
    created: list[str] = []
    for connector_id in rule.connector_ids or []:
        connector = labels.get(connector_id)
        delivery = NotificationDelivery(
            id=new_id(),
            event_id=event.id,
            connector_id=connector_id,
            connector_label=(connector or {}).get("name", "") or "Unknown connector",
            status="skipped" if not connector else status,
            detail="Connector no longer exists" if not connector else detail,
            next_attempt_at=utcnow() if (connector and status == "pending") else None,
        )
        db.add(delivery)
        if connector and status == "pending":
            created.append(delivery.id)
    return created


async def publish(
    db: AsyncSession,
    type: str,
    severity: str,
    title: str,
    body: str,
    facts: dict[str, Any] | None = None,
    schedule_id: str | None = None,
    run_id: str | None = None,
    vm_id: str | None = None,
    group_id: str | None = None,
    connection_id: str | None = None,
    fingerprint: str | None = None,
) -> NotificationEvent | None:
    """Always records the event (the in-app feed), then routes it to whatever rules match."""
    severity = severity_of(severity)
    if fingerprint:
        existing = await db.scalar(select(NotificationEvent).where(NotificationEvent.fingerprint == fingerprint))
        if existing:
            return existing
    event = NotificationEvent(
        id=new_id(),
        type=type,
        severity=severity,
        title=title[:512],
        body=body,
        facts_json=facts or {},
        schedule_id=schedule_id,
        run_id=run_id,
        vm_id=vm_id,
        group_id=group_id,
        connection_id=connection_id,
        fingerprint=fingerprint,
    )
    try:
        # The pre-check is the cheap common path, but another scheduler can commit the same
        # fingerprint between that SELECT and this INSERT. Keep the unique constraint as the
        # authority and contain a losing insert inside a savepoint so the caller's transaction
        # remains usable.
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError:
        if fingerprint:
            existing = await db.scalar(select(NotificationEvent).where(NotificationEvent.fingerprint == fingerprint))
            if existing:
                return existing
        raise

    rules = list((await db.scalars(select(NotificationRule).where(NotificationRule.enabled.is_(True)))).all())
    candidates = [rule for rule in rules if delivers_immediately(rule, type)]
    tree = await load_tree(db) if any(rule.scope_group_id for rule in candidates) else None
    now = utcnow()
    labels = await _connector_labels() if candidates else {}
    queued: list[str] = []
    for rule in candidates:
        if not matches(rule, type, severity, group_id, tree):
            continue
        if in_quiet_hours(rule, severity, now):
            _queue_deliveries(db, event, rule, labels, "skipped", "Suppressed by quiet hours")
            continue
        if await _throttled(db, rule, type, now):
            _queue_deliveries(db, event, rule, labels, "skipped", f"Throttled for {rule.throttle_minutes} minute(s)")
            continue
        queued.extend(_queue_deliveries(db, event, rule, labels, "pending", "Queued for delivery"))
    await db.commit()
    delivery_service.submit_many(queued)
    return event


async def publish_background(**kwargs: Any) -> None:
    """Publishing must never break the caller's workflow."""
    try:
        async with SessionLocal() as session:
            await publish(session, **kwargs)
    except Exception:
        logger.exception("Publishing the %s notification failed", kwargs.get("type"))


# -- daily digest ------------------------------------------------------


def digest_due_at(rule: NotificationRule, now: datetime) -> datetime | None:
    """The most recent digest boundary that has passed, or None when it was already delivered."""
    zone = _zone(rule.digest_timezone or "UTC")
    local_now = now.astimezone(zone)
    hour = min(max(int(rule.digest_hour or 0), 0), 23)
    boundary = datetime.combine(local_now.date(), time(hour), tzinfo=zone).astimezone(timezone.utc)
    if boundary > now:
        boundary = datetime.combine(local_now.date() - timedelta(days=1), time(hour), tzinfo=zone).astimezone(timezone.utc)
    last = rule.last_digest_at.replace(tzinfo=rule.last_digest_at.tzinfo or timezone.utc) if rule.last_digest_at else None
    return None if last and last >= boundary else boundary


def summarize(events: list[NotificationEvent]) -> dict[str, Any]:
    runs = [item for item in events if item.type.startswith("run.")]
    totals = {"waves": len(runs), "vm_count": 0, "succeeded": 0, "failed": 0, "missed": sum(item.type == "schedule.missed" for item in events)}
    applications: dict[str, dict[str, int]] = {}
    failed_names: list[str] = []
    for item in runs:
        facts = item.facts_json or {}
        counts = {key: int(facts.get(key) or 0) for key in ("vm_count", "succeeded", "failed")}
        for key, value in counts.items():
            totals[key] += value
        bucket = applications.setdefault(str(facts.get("application") or "Unassigned"), {"waves": 0, "succeeded": 0, "failed": 0})
        bucket["waves"] += 1
        bucket["succeeded"] += counts["succeeded"]
        bucket["failed"] += counts["failed"]
        failed_names.extend(str(name) for name in (facts.get("failed_vm_names") or []))
    return {**totals, "applications": applications, "failed_vm_names": failed_names[:50]}


def digest_body(summary: dict[str, Any], since: datetime, until: datetime) -> str:
    lines = [
        f"Window: {since.astimezone(timezone.utc).isoformat()} to {until.astimezone(timezone.utc).isoformat()}",
        f"Waves run: {summary['waves']} · VMs started: {summary['vm_count']} · Succeeded: {summary['succeeded']} · Failed: {summary['failed']} · Missed schedules: {summary['missed']}",
    ]
    for name, bucket in sorted(summary["applications"].items()):
        lines.append(f"  {name}: {bucket['waves']} wave(s), {bucket['succeeded']} succeeded, {bucket['failed']} failed")
    if summary["failed_vm_names"]:
        lines.append(f"Failed VMs: {', '.join(summary['failed_vm_names'])}")
    return "\n".join(lines)


async def run_daily_digests(db: AsyncSession, now: datetime | None = None) -> list[str]:
    """One summary message per due rule; `last_digest_at` makes a restart safe."""
    now = now or utcnow()
    rules = list((await db.scalars(select(NotificationRule).where(NotificationRule.enabled.is_(True), NotificationRule.digest_mode == "daily"))).all())
    if not rules:
        return []
    labels = await _connector_labels()
    tree = await load_tree(db) if any(rule.scope_group_id for rule in rules) else None
    queued: list[str] = []
    for rule in rules:
        boundary = digest_due_at(rule, now)
        if boundary is None:
            continue
        since = rule.last_digest_at.replace(tzinfo=rule.last_digest_at.tzinfo or timezone.utc) if rule.last_digest_at else boundary - timedelta(days=1)
        events = list((await db.scalars(
            select(NotificationEvent).where(NotificationEvent.created_at > since, NotificationEvent.created_at <= now, NotificationEvent.type != DIGEST_EVENT).order_by(NotificationEvent.created_at)
        )).all())
        selected = [item for item in events if matches(rule, item.type, item.severity, item.group_id, tree)]
        rule.last_digest_at = now
        if not selected:
            continue
        summary = summarize(selected)
        severity = "critical" if summary["missed"] else "error" if summary["failed"] else "info"
        digest = NotificationEvent(
            id=new_id(),
            type=DIGEST_EVENT,
            severity=severity,
            title=f"Azure VM Scheduler daily digest — {summary['succeeded']}/{summary['vm_count']} succeeded · {summary['failed']} failed",
            body=digest_body(summary, since, now),
            facts_json={"schedule_name": rule.name, "vm_count": summary["vm_count"], "succeeded": summary["succeeded"], "failed": summary["failed"], "failed_vm_names": summary["failed_vm_names"], "waves": summary["waves"], "missed": summary["missed"]},
        )
        db.add(digest)
        await db.flush()
        queued.extend(_queue_deliveries(db, digest, rule, labels, "pending", "Queued for delivery"))
    await db.commit()
    return queued


def read_receipt_exists(user_id: str):
    return select(NotificationEventRead.event_id).where(
        NotificationEventRead.user_id == user_id,
        NotificationEventRead.event_id == NotificationEvent.id,
    ).exists()


async def unread_count(db: AsyncSession, user_id: str) -> int:
    """Unread events for one account, independent of every other viewer."""
    return int(await db.scalar(
        select(func.count()).select_from(NotificationEvent).where(~read_receipt_exists(user_id))
    ) or 0)


async def read_event_ids(db: AsyncSession, user_id: str, event_ids: list[str]) -> set[str]:
    if not event_ids:
        return set()
    return set((await db.scalars(select(NotificationEventRead.event_id).where(
        NotificationEventRead.user_id == user_id,
        NotificationEventRead.event_id.in_(event_ids),
    ))).all())


async def mark_events_read(db: AsyncSession, user_id: str, event_ids: list[str]) -> int:
    """Insert read receipts idempotently on both supported database dialects."""
    unique = list(dict.fromkeys(event_ids))
    if not unique:
        return 0
    values = [{"event_id": event_id, "user_id": user_id, "read_at": utcnow()} for event_id in unique]
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(NotificationEventRead).values(values).on_conflict_do_nothing(
        index_elements=[NotificationEventRead.event_id, NotificationEventRead.user_id]
    )
    result = await db.execute(statement)
    return max(int(getattr(result, "rowcount", 0) or 0), 0)
