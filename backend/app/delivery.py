"""Bounded delivery pipeline: a slow connector can never stall VM starts."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select

from .config import get_settings
from .connectors.base import ConnectorError, Message, sanitize_detail
from .connectors.registry import get_connector, send_via_connector
from .connectors.servicenow import correlation_id, resolves_automatically
from .database import SessionLocal
from .models import NotificationDelivery, NotificationEvent, utcnow
from .templating import build_message


logger = logging.getLogger(__name__)

RESOLVING_EVENTS = {"run.succeeded"}
FAILURE_EVENTS = {"run.failed", "run.partially_failed", "run.timed_out", "schedule.missed", "connection.unhealthy"}


def is_transient(exc: Exception) -> bool:
    """Only timeouts, throttling, 5xx, and connection errors are worth another attempt."""
    if isinstance(exc, ConnectorError):
        return exc.transient
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


def retry_delay(attempts: int) -> float:
    settings = get_settings()
    base = settings.delivery_retry_base_seconds * (2 ** max(attempts - 1, 0))
    return min(float(base), float(settings.delivery_retry_max_seconds)) + random.uniform(0, 5)


def message_for(event: NotificationEvent, connector: dict[str, Any]) -> Message:
    facts = dict(event.facts_json or {})
    resolve = connector["type"] == "servicenow" and event.type in RESOLVING_EVENTS and resolves_automatically(connector.get("config") or {})
    return build_message(
        event.type,
        event.severity,
        event.title,
        event.body,
        facts,
        run_id=event.run_id,
        correlation_key=correlation_id(event.schedule_id),
        resolve=resolve,
    )


class DeliveryService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.settings.notification_queue_size)

    async def start(self) -> None:
        self._stop.clear()
        for index in range(self.settings.delivery_concurrency):
            self._tasks.append(asyncio.create_task(self._worker(), name=f"azureops-delivery-{index}"))
        self._tasks.append(asyncio.create_task(self._sweep_loop(), name="azureops-delivery-sweeper"))

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

    def submit(self, delivery_id: str) -> None:
        """Never blocks the caller; a full queue falls back to the sweeper."""
        try:
            self._queue.put_nowait(delivery_id)
        except asyncio.QueueFull:
            logger.warning("Delivery queue is full; %s will be retried by the sweeper", delivery_id)

    def submit_many(self, delivery_ids: list[str]) -> None:
        for delivery_id in delivery_ids:
            self.submit(delivery_id)

    async def _worker(self) -> None:
        while not self._stop.is_set():
            delivery_id = await self._queue.get()
            try:
                await self.deliver(delivery_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Notification delivery failed for %s", delivery_id)
            finally:
                self._queue.task_done()

    async def _sweep_loop(self) -> None:
        while not self._stop.is_set():
            try:
                async with SessionLocal() as session:
                    now = utcnow()
                    due = (await session.scalars(
                        select(NotificationDelivery.id)
                        .where(NotificationDelivery.status == "pending", or_(NotificationDelivery.next_attempt_at.is_(None), NotificationDelivery.next_attempt_at <= now))
                        .order_by(NotificationDelivery.created_at)
                        .limit(200)
                    )).all()
                self.submit_many(list(due))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Delivery sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.delivery_poll_seconds)
            except TimeoutError:
                continue

    async def deliver(self, delivery_id: str) -> None:
        async with SessionLocal() as session:
            delivery = await session.get(NotificationDelivery, delivery_id)
            if not delivery or delivery.status != "pending":
                return
            event = await session.get(NotificationEvent, delivery.event_id)
            if not event:
                delivery.status = "skipped"
                delivery.detail = "The originating event no longer exists"
                await session.commit()
                return
            connector_id = delivery.connector_id
            attempts = delivery.attempts + 1

        connector = await get_connector(connector_id)
        if not connector:
            await self._finish(delivery_id, "skipped", attempts, "Connector no longer exists")
            return
        if connector.get("disabled"):
            await self._finish(delivery_id, "skipped", attempts, "Connector is disabled")
            return
        try:
            result = await send_via_connector(connector, message_for(event, connector))
        except Exception as exc:
            await self._record_failure(delivery_id, attempts, exc)
            return
        status = "skipped" if result.get("skipped") else "sent"
        await self._finish(delivery_id, status, attempts, str(result.get("detail") or "Delivered"), str(result.get("external_ref") or ""))

    async def _record_failure(self, delivery_id: str, attempts: int, exc: Exception) -> None:
        detail = sanitize_detail(exc)
        if is_transient(exc) and attempts < self.settings.delivery_max_attempts:
            async with SessionLocal() as session:
                delivery = await session.get(NotificationDelivery, delivery_id)
                if delivery:
                    delivery.attempts = attempts
                    delivery.detail = f"Attempt {attempts} failed, retrying: {detail}"
                    delivery.next_attempt_at = utcnow() + timedelta(seconds=retry_delay(attempts))
                    await session.commit()
            return
        await self._finish(delivery_id, "failed", attempts, detail)

    async def _finish(self, delivery_id: str, status: str, attempts: int, detail: str, external_ref: str = "") -> None:
        async with SessionLocal() as session:
            delivery = await session.get(NotificationDelivery, delivery_id)
            if not delivery:
                return
            delivery.status = status
            delivery.attempts = attempts
            delivery.detail = detail[:2000]
            delivery.next_attempt_at = None
            delivery.external_ref = external_ref[:200] or delivery.external_ref
            delivery.sent_at = utcnow() if status == "sent" else delivery.sent_at
            await session.commit()


delivery_service = DeliveryService()
