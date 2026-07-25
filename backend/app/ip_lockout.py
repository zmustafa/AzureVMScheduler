"""Per-IP brute-force protection.

The per-account lockout in ``auth`` stops someone hammering one username. It does nothing against
the more common attack: one password sprayed across many usernames from a single source. This
limiter counts failures per IP over a sliding window and locks that IP out for a cooldown, then
releases it automatically.

State lives in the database rather than memory so a restart cannot be used to clear it, and so the
counter is meaningful even though the app runs a single replica today.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import LoginThrottle, SecurityPolicy, utcnow


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC so comparisons never raise."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def client_ip(request) -> str | None:
    """The caller's address, trusting a forwarded header only when configured to.

    Behind Azure Container Apps the peer is always the ingress, so ``X-Forwarded-For`` is the only
    way to see the real client. Off a trusted proxy the header is attacker-controlled and must be
    ignored, or an attacker rotates it to reset their own counter.
    """
    from .config import get_settings

    if get_settings().trust_forwarded_headers:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded[:64]
    return request.client.host[:64] if request and request.client else None


async def check(db: AsyncSession, policy: SecurityPolicy, ip: str | None) -> int | None:
    """Seconds remaining if this IP is locked out, otherwise None."""
    if not ip or not policy.ip_lockout_enabled:
        return None
    row = await db.get(LoginThrottle, ip)
    locked_until = _aware(row.locked_until) if row else None
    if locked_until and locked_until > utcnow():
        return max(1, int((locked_until - utcnow()).total_seconds()))
    return None


async def record_failure(db: AsyncSession, policy: SecurityPolicy, ip: str | None) -> None:
    """Count one failed sign-in, locking the IP out once the threshold is reached."""
    if not ip or not policy.ip_lockout_enabled:
        return
    now = utcnow()
    row = await db.get(LoginThrottle, ip)
    if row is None:
        row = LoginThrottle(ip=ip, fail_count=0, window_start=now)
        db.add(row)
    window_start = _aware(row.window_start)
    # Only recent failures count, so an occasional typo never accumulates into a lockout.
    if window_start is None or (now - window_start).total_seconds() > policy.ip_lockout_window_seconds:
        row.window_start = now
        row.fail_count = 0
    row.fail_count += 1
    if row.fail_count >= policy.ip_lockout_attempts:
        row.locked_until = now + timedelta(seconds=policy.ip_lockout_seconds)
        row.fail_count = 0
        row.window_start = now
    row.updated_at = now


async def clear(db: AsyncSession, ip: str | None) -> None:
    """Forget an IP after a successful sign-in from it."""
    if ip:
        await db.execute(delete(LoginThrottle).where(LoginThrottle.ip == ip))


async def purge_expired(db: AsyncSession) -> int:
    """Drop rows that are neither locked nor recently active."""
    cutoff = utcnow() - timedelta(days=1)
    stale = (await db.scalars(select(LoginThrottle.ip).where(LoginThrottle.updated_at < cutoff))).all()
    if stale:
        await db.execute(delete(LoginThrottle).where(LoginThrottle.ip.in_(stale)))
    return len(stale)
