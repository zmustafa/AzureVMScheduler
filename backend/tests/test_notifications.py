from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app import connections
from app.connectors import registry as registry_module
from app.hierarchy import GroupTree
from app.models import Group, NotificationDelivery, NotificationEvent, NotificationRule, User, new_id
from app.notifications import (
    delivers_immediately,
    digest_due_at,
    digest_body,
    in_quiet_hours,
    matches,
    publish,
    run_daily_digests,
    summarize,
)


def _rule(**overrides) -> NotificationRule:
    values = {
        "id": new_id(),
        "name": "Rule",
        "enabled": True,
        "event_types": [],
        "min_severity": "warning",
        "scope_group_id": None,
        "include_subtree": True,
        "connector_ids": [],
        "in_app": True,
        "digest_mode": "immediate",
        "digest_hour": 8,
        "digest_timezone": "America/New_York",
        "quiet_hours_start": "",
        "quiet_hours_end": "",
        "quiet_hours_timezone": "America/New_York",
        "critical_ignores_quiet_hours": True,
        "throttle_minutes": 0,
    }
    values.update(overrides)
    return NotificationRule(**values)


def _tree() -> GroupTree:
    application = Group(id="app", parent_id=None, name="Payments", path="/app/", depth=0, enabled=True)
    ring = Group(id="ring", parent_id="app", name="Ring 1", path="/app/ring/", depth=1, enabled=True)
    other = Group(id="other", parent_id=None, name="Billing", path="/other/", depth=0, enabled=True)
    return GroupTree({"app": application, "ring": ring, "other": other})


@pytest.fixture
def registry_files(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "_paths", lambda: (tmp_path / "azure_connections.json", tmp_path / "secret.key"))
    monkeypatch.setattr(registry_module, "_path", lambda: tmp_path / "connectors.json")
    return tmp_path


@pytest_asyncio.fixture
async def connector(registry_files):
    return await registry_module.upsert_connector({
        "name": "Ops webhook",
        "type": "webhook",
        "mode": "https",
        "config": {"url": "https://hooks.example.com/azureops", "signing_secret": "super-secret-value"},
    })


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    """Deliveries are asserted from the database; the worker pool never runs in tests."""
    monkeypatch.setattr("app.notifications.delivery_service.submit_many", lambda ids: None)


async def _deliveries(session, event_id: str | None = None) -> list[NotificationDelivery]:
    from sqlalchemy import select

    statement = select(NotificationDelivery)
    if event_id:
        statement = statement.where(NotificationDelivery.event_id == event_id)
    return list((await session.scalars(statement)).all())


# -- rule matching -----------------------------------------------------


def test_severity_threshold_filters_quieter_events() -> None:
    rule = _rule(min_severity="error")
    assert not matches(rule, "run.failed", "warning")
    assert matches(rule, "run.failed", "error")
    assert matches(rule, "run.failed", "critical")


def test_event_type_allowlist_is_empty_for_any() -> None:
    assert matches(_rule(), "run.failed", "error")
    assert matches(_rule(event_types=["run.failed"]), "run.failed", "error")
    assert not matches(_rule(event_types=["run.succeeded"]), "run.failed", "error")


def test_disabled_rules_never_match() -> None:
    assert not matches(_rule(enabled=False), "run.failed", "critical")


def test_group_scope_inherits_down_the_subtree() -> None:
    tree = _tree()
    scoped = _rule(scope_group_id="app", include_subtree=True)
    assert matches(scoped, "run.failed", "error", "app", tree)
    assert matches(scoped, "run.failed", "error", "ring", tree)
    assert not matches(scoped, "run.failed", "error", "other", tree)


def test_group_scope_without_subtree_only_matches_the_node() -> None:
    tree = _tree()
    scoped = _rule(scope_group_id="app", include_subtree=False)
    assert matches(scoped, "run.failed", "error", "app", tree)
    assert not matches(scoped, "run.failed", "error", "ring", tree)


def test_unscoped_rules_match_events_without_a_group() -> None:
    assert matches(_rule(), "connection.unhealthy", "error", None, None)
    assert not matches(_rule(scope_group_id="app"), "connection.unhealthy", "error", None, _tree())


# -- quiet hours -------------------------------------------------------


def test_quiet_hours_hold_across_a_dst_boundary() -> None:
    rule = _rule(quiet_hours_start="22:00", quiet_hours_end="06:00", quiet_hours_timezone="America/New_York")
    winter_night = datetime(2027, 1, 15, 6, 0, tzinfo=timezone.utc)  # 01:00 EST
    summer_night = datetime(2027, 7, 15, 5, 0, tzinfo=timezone.utc)  # 01:00 EDT
    winter_day = datetime(2027, 1, 15, 20, 0, tzinfo=timezone.utc)  # 15:00 EST
    summer_day = datetime(2027, 7, 15, 19, 0, tzinfo=timezone.utc)  # 15:00 EDT
    assert in_quiet_hours(rule, "error", winter_night)
    assert in_quiet_hours(rule, "error", summer_night)
    assert not in_quiet_hours(rule, "error", winter_day)
    assert not in_quiet_hours(rule, "error", summer_day)


def test_critical_events_can_ignore_quiet_hours() -> None:
    night = datetime(2027, 1, 15, 6, 0, tzinfo=timezone.utc)
    assert not in_quiet_hours(_rule(quiet_hours_start="22:00", quiet_hours_end="06:00"), "critical", night)
    assert in_quiet_hours(_rule(quiet_hours_start="22:00", quiet_hours_end="06:00", critical_ignores_quiet_hours=False), "critical", night)


def test_blank_quiet_hours_never_suppress() -> None:
    assert not in_quiet_hours(_rule(), "info", datetime(2027, 1, 15, 6, 0, tzinfo=timezone.utc))


# -- digest routing ----------------------------------------------------


def test_immediate_mode_suppresses_per_vm_noise() -> None:
    immediate, per_vm, daily = _rule(), _rule(digest_mode="per_vm"), _rule(digest_mode="daily")
    assert delivers_immediately(immediate, "run.partially_failed")
    assert not delivers_immediately(immediate, "vm.start_failed")
    assert delivers_immediately(per_vm, "vm.start_failed")
    assert not delivers_immediately(daily, "run.failed")


def test_digest_boundary_is_dst_safe_and_never_double_sends() -> None:
    rule = _rule(digest_mode="daily", digest_hour=8, digest_timezone="America/New_York")
    after_spring_forward = datetime(2027, 3, 14, 13, 0, tzinfo=timezone.utc)  # 09:00 EDT
    boundary = digest_due_at(rule, after_spring_forward)
    assert boundary == datetime(2027, 3, 14, 12, 0, tzinfo=timezone.utc)
    rule.last_digest_at = boundary
    assert digest_due_at(rule, after_spring_forward + timedelta(minutes=30)) is None
    autumn = datetime(2027, 11, 8, 14, 0, tzinfo=timezone.utc)  # 09:00 EST
    assert digest_due_at(_rule(digest_mode="daily", digest_hour=8), autumn) == datetime(2027, 11, 8, 13, 0, tzinfo=timezone.utc)


def test_digest_before_the_hour_uses_yesterdays_boundary() -> None:
    rule = _rule(digest_mode="daily", digest_hour=8, digest_timezone="UTC")
    assert digest_due_at(rule, datetime(2027, 5, 4, 3, 0, tzinfo=timezone.utc)) == datetime(2027, 5, 3, 8, 0, tzinfo=timezone.utc)


def test_summary_rolls_up_per_application() -> None:
    events = [
        NotificationEvent(id="a", type="run.partially_failed", severity="warning", facts_json={"application": "Payments", "vm_count": 30, "succeeded": 24, "failed": 6, "failed_vm_names": ["vm-1"]}),
        NotificationEvent(id="b", type="run.succeeded", severity="info", facts_json={"application": "Billing", "vm_count": 4, "succeeded": 4, "failed": 0}),
        NotificationEvent(id="c", type="schedule.missed", severity="critical", facts_json={}),
    ]
    summary = summarize(events)
    assert summary["waves"] == 2
    assert (summary["vm_count"], summary["succeeded"], summary["failed"], summary["missed"]) == (34, 28, 6, 1)
    assert summary["applications"]["Payments"] == {"waves": 1, "succeeded": 24, "failed": 6}
    assert "Payments: 1 wave(s), 24 succeeded, 6 failed" in digest_body(summary, datetime(2027, 5, 3, tzinfo=timezone.utc), datetime(2027, 5, 4, tzinfo=timezone.utc))


# -- publishing --------------------------------------------------------


@pytest.mark.asyncio
async def test_events_reach_the_feed_even_with_no_rules(session) -> None:
    event = await publish(session, type="run.failed", severity="error", title="Wave failed", body="body")
    assert event and event.read is False
    assert await _deliveries(session) == []


@pytest.mark.asyncio
async def test_stop_attempt_notification_body_names_the_stop_action(session, monkeypatch) -> None:
    from app import scheduling
    from app.models import VmAttempt

    attempt = VmAttempt(
        id=new_id(), vm_resource_id="/subscriptions/s/resourceGroups/r/providers/Microsoft.Compute/virtualMachines/vm-stop",
        action="stop", status="skipped", message="",
    )
    captured: dict[str, object] = {}

    async def capture_publish(_session, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(scheduling, "publish", capture_publish)
    await scheduling._publish_attempt_event(session, attempt)

    assert captured["body"] == "The stop attempt for vm-stop ended as skipped."


@pytest.mark.asyncio
async def test_fingerprint_dedup_publishes_once(session) -> None:
    first = await publish(session, type="schedule.missed", severity="critical", title="Missed", body="", fingerprint="schedule.missed:s1:2027-05-04T12:00:00+00:00")
    second = await publish(session, type="schedule.missed", severity="critical", title="Missed", body="", fingerprint="schedule.missed:s1:2027-05-04T12:00:00+00:00")
    assert first and second and first.id == second.id
    from sqlalchemy import func, select

    assert await session.scalar(select(func.count()).select_from(NotificationEvent)) == 1


@pytest.mark.asyncio
async def test_fingerprint_dedup_survives_a_concurrent_insert(session, monkeypatch) -> None:
    """A stale pre-check must lose cleanly to the database unique constraint."""
    fingerprint = "schedule.missed:s1:2027-05-04T12:00:00+00:00"
    existing = NotificationEvent(
        id=new_id(), type="schedule.missed", severity="critical", title="Missed", body="",
        fingerprint=fingerprint,
    )
    session.add(existing)
    await session.commit()

    real_scalar = session.scalar
    calls = 0

    async def stale_once(statement):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return await real_scalar(statement)

    monkeypatch.setattr(session, "scalar", stale_once)
    result = await publish(
        session, type="schedule.missed", severity="critical", title="Missed", body="",
        fingerprint=fingerprint,
    )

    assert result is not None and result.id == existing.id
    from sqlalchemy import func, select

    assert await real_scalar(select(func.count()).select_from(NotificationEvent)) == 1


@pytest.mark.asyncio
async def test_read_receipts_are_private_to_each_user(session) -> None:
    from test_runs import api_client

    alice = User(id=new_id(), username="alice", role="viewer")
    bob = User(id=new_id(), username="bob", role="viewer")
    session.add_all([alice, bob])
    await session.commit()
    event = await publish(session, type="run.failed", severity="error", title="Wave failed", body="body")
    assert event is not None

    async with api_client(session, alice) as client:
        assert (await client.get("/api/notifications/unread-count")).json()["count"] == 1
        marked = await client.post(f"/api/notifications/{event.id}/read")
        assert marked.status_code == 200
        assert (await client.get("/api/notifications/unread-count")).json()["count"] == 0
        assert (await client.get("/api/notifications")).json()["items"][0]["read"] is True

    async with api_client(session, bob) as client:
        assert (await client.get("/api/notifications/unread-count")).json()["count"] == 1
        body = (await client.get("/api/notifications", params={"unread_only": "true"})).json()
        assert body["unread"] == 1
        assert body["items"][0]["read"] is False


@pytest.mark.asyncio
async def test_mark_all_is_idempotent_and_private_to_the_current_user(session) -> None:
    from test_runs import api_client

    alice = User(id=new_id(), username="alice-all", role="viewer")
    bob = User(id=new_id(), username="bob-all", role="viewer")
    session.add_all([alice, bob])
    await session.commit()
    await publish(session, type="run.failed", severity="error", title="First", body="")
    await publish(session, type="run.failed", severity="error", title="Second", body="")

    async with api_client(session, alice) as client:
        first = await client.post("/api/notifications/read-all")
        second = await client.post("/api/notifications/read-all")
        assert first.json()["count"] == 2
        assert second.json()["count"] == 0
        assert (await client.get("/api/notifications/unread-count")).json()["count"] == 0

    async with api_client(session, bob) as client:
        assert (await client.get("/api/notifications/unread-count")).json()["count"] == 2


@pytest.mark.asyncio
async def test_startup_converts_legacy_global_read_flags_once(session) -> None:
    from app.database import _backfill_notification_read_receipts
    from app.models import NotificationEventRead

    existing = User(id=new_id(), username="existing", role="viewer")
    event = NotificationEvent(id=new_id(), type="run.failed", severity="error", title="Old", body="", read=True)
    session.add_all([existing, event])
    await session.commit()

    await session.run_sync(lambda sync_session: _backfill_notification_read_receipts(sync_session.connection()))
    await session.commit()
    assert await session.get(NotificationEventRead, (event.id, existing.id)) is not None
    await session.refresh(event)
    assert event.read is False

    later = User(id=new_id(), username="later", role="viewer")
    session.add(later)
    await session.commit()
    await session.run_sync(lambda sync_session: _backfill_notification_read_receipts(sync_session.connection()))
    await session.commit()
    assert await session.get(NotificationEventRead, (event.id, later.id)) is None


@pytest.mark.asyncio
async def test_thirty_vm_ring_produces_one_message_by_default(session, connector) -> None:
    session.add(_rule(name="Ops", connector_ids=[connector["id"]], min_severity="warning"))
    await session.commit()
    wave = await publish(
        session,
        type="run.partially_failed",
        severity="warning",
        title="Payments partially failed",
        body="24/30 succeeded",
        facts={"vm_count": 30, "succeeded": 24, "failed": 6},
        run_id="run-1",
    )
    for index in range(6):
        await publish(session, type="vm.start_failed", severity="error", title=f"vm-{index} failed", body="", run_id="run-1")
    deliveries = await _deliveries(session)
    assert len(deliveries) == 1
    assert deliveries[0].event_id == (wave.id if wave else "")
    assert deliveries[0].status == "pending"


@pytest.mark.asyncio
async def test_per_vm_mode_fans_out_to_every_failure(session, connector) -> None:
    session.add(_rule(name="Per VM", connector_ids=[connector["id"]], digest_mode="per_vm", min_severity="warning"))
    await session.commit()
    await publish(session, type="run.partially_failed", severity="warning", title="Wave", body="", facts={"vm_count": 30, "succeeded": 24, "failed": 6})
    for index in range(6):
        await publish(session, type="vm.start_failed", severity="error", title=f"vm-{index} failed", body="")
    assert len(await _deliveries(session)) == 7


@pytest.mark.asyncio
async def test_daily_mode_defers_to_the_digest(session, connector) -> None:
    session.add(_rule(name="Digest", connector_ids=[connector["id"]], digest_mode="daily", digest_hour=8, digest_timezone="UTC", min_severity="info"))
    await session.commit()
    await publish(session, type="run.partially_failed", severity="warning", title="Wave", body="", facts={"application": "Payments", "vm_count": 30, "succeeded": 24, "failed": 6})
    assert await _deliveries(session) == []

    # Anchor on the real clock: a fixed hour makes this fail whenever the suite runs after it.
    after_publish = datetime.now(timezone.utc) + timedelta(minutes=1)
    queued = await run_daily_digests(session, after_publish)
    deliveries = await _deliveries(session)
    assert len(queued) == 1 and len(deliveries) == 1
    digest = await session.get(NotificationEvent, deliveries[0].event_id)
    assert digest and "24/30 succeeded" in digest.title
    assert await run_daily_digests(session, after_publish + timedelta(minutes=30)) == []


@pytest.mark.asyncio
async def test_throttle_skips_a_repeat_within_the_window(session, connector) -> None:
    session.add(_rule(name="Throttled", connector_ids=[connector["id"]], throttle_minutes=30, min_severity="warning"))
    await session.commit()
    await publish(session, type="run.failed", severity="error", title="First", body="")
    await publish(session, type="run.failed", severity="error", title="Second", body="")
    statuses = sorted(item.status for item in await _deliveries(session))
    assert statuses == ["pending", "skipped"]
    assert any("Throttled" in item.detail for item in await _deliveries(session))


@pytest.mark.asyncio
async def test_quiet_hours_record_a_skipped_delivery(session, connector) -> None:
    # Centre the window on the current time: "00:00–23:59" leaves a one-minute gap that
    # this test falls into once a day.
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")
    session.add(_rule(name="Quiet", connector_ids=[connector["id"]], quiet_hours_start=start, quiet_hours_end=end, quiet_hours_timezone="UTC", min_severity="warning"))
    await session.commit()
    await publish(session, type="run.failed", severity="error", title="Overnight", body="")
    deliveries = await _deliveries(session)
    assert [item.status for item in deliveries] == ["skipped"]
    assert "quiet hours" in deliveries[0].detail


@pytest.mark.asyncio
async def test_group_scope_is_enforced_when_publishing(session, connector) -> None:
    application = Group(id="app", parent_id=None, name="Payments", path="/app/", depth=0)
    ring = Group(id="ring", parent_id="app", name="Ring 1", path="/app/ring/", depth=1)
    outside = Group(id="other", parent_id=None, name="Billing", path="/other/", depth=0)
    session.add_all([application, ring, outside, _rule(name="Payments only", connector_ids=[connector["id"]], scope_group_id="app")])
    await session.commit()
    await publish(session, type="run.failed", severity="error", title="Ring failed", body="", group_id="ring")
    await publish(session, type="run.failed", severity="error", title="Billing failed", body="", group_id="other")
    assert len(await _deliveries(session)) == 1
