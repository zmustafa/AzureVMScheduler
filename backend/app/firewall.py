"""Network admission control: only listed addresses may reach the app.

Account lockout and the per-IP throttle blunt brute force but do not remove the exposure — with
public ingress, anyone on the internet still gets to *try*. This filters by source address before
any routing, session lookup or password verification happens.

It is deliberately all-or-nothing: when it is on, every path is filtered except the health probes.
An earlier design let it cover only the sign-in endpoints, which read as a safety feature but was
really a trap — it invited "the firewall is on" to mean something different from what it meant, and
the whole point of an admission control is that there is nothing to reason about.

Three properties shape the implementation:

* **It must be fast.** The check sits in front of every request including static assets, so it
  reads an in-memory snapshot and never touches the database. The snapshot is refreshed at
  startup, after every write, and on the scheduler's poll cycle.
* **It must not be an amplification target.** An unauthenticated attacker chooses how many
  requests they send, so a blocked request must not cause a database write. Blocks accumulate in
  a bounded in-memory buffer that the scheduler drains and coalesces.
* **It must fail open, never closed.** A snapshot that was never loaded, an empty rule list, or a
  kill switch in the environment all mean "allow". Being locked out of your own management plane
  is a worse outcome than an hour of unnecessary exposure.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import IpAllowRule, IpBlockEvent, SecurityPolicy, aware as _aware, new_id, utcnow
from .netaddr import client_ip, matches, parse_address, parse_network

logger = logging.getLogger(__name__)

MODES = ("disabled", "audit", "enforce")

#: Default commit-confirm window. Long enough to notice you are still connected, short enough that
#: waiting it out is a reasonable recovery plan.
DEFAULT_CONFIRM_MINUTES = 15

#: The only exemption. Container Apps probes the readiness endpoint from platform addresses nobody
#: would think to allowlist, and a failing probe restarts the container forever.
_EXEMPT_PATHS = frozenset({"/api/health", "/health", "/healthz", "/readyz"})

#: The credential surface. Not used for filtering — everything is filtered — but it labels a blocked
#: request in the log, so "someone is guessing passwords" reads differently from "someone loaded a
#: page they should not have".
_AUTH_PREFIXES = ("/api/auth/login", "/api/auth/change-password", "/api/auth/oidc/", "/api/auth/saml/")

#: Blocked sources awaiting coalescing. Bounded: the buffer must never grow with attacker traffic.
_BLOCK_BUFFER: deque[tuple[str, str, str, bool, datetime]] = deque(maxlen=2000)
#: Rule ids seen since the last flush, so "last used" is maintained without a write per request.
_RULE_HITS: set[str] = set()

BLOCK_EVENT_RETENTION_DAYS = 7
#: Two hits from one address inside this window are one row.
_COALESCE_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class Snapshot:
    """The compiled policy the middleware reads. Immutable, replaced wholesale on refresh."""

    mode: str = "disabled"
    #: (rule id, network). Bootstrap networks carry a None id — they are not database rows.
    networks: tuple[tuple[str | None, object], ...] = ()
    confirm_by: datetime | None = None

    @property
    def active(self) -> bool:
        """Whether this snapshot filters anything at all.

        An `enforce` mode with no networks is always an accident — someone deleted the last rule —
        and treating it as "block the world" would brick the deployment. It fails open instead.
        """
        return self.mode in ("audit", "enforce") and bool(self.networks)


_snapshot = Snapshot()
_loaded = False
_last_prune: datetime | None = None


def snapshot() -> Snapshot:
    return _snapshot


def is_loaded() -> bool:
    return _loaded


def reset_for_tests() -> None:
    global _snapshot, _loaded, _last_prune
    _snapshot, _loaded, _last_prune = Snapshot(), False, None
    _BLOCK_BUFFER.clear()
    _RULE_HITS.clear()


def bootstrap_networks() -> list:
    """Environment-only ranges that are always allowed. The break-glass path.

    An administrator who has locked themselves out restores access with a container restart and no
    database surgery — and, unlike a database rule, this cannot be edited away from the UI.
    """
    compiled = []
    for entry in get_settings().bootstrap_networks:
        try:
            compiled.append(parse_network(entry))
        except ValueError:
            logger.warning("Ignoring unparseable IP_ALLOWLIST_BOOTSTRAP entry: %s", entry)
    return compiled


async def refresh(db: AsyncSession) -> Snapshot:
    """Recompile the snapshot from the database. Called at startup and after every write."""
    global _snapshot, _loaded

    policy = await db.scalar(select(SecurityPolicy).where(SecurityPolicy.id == 1))
    rules = (await db.scalars(select(IpAllowRule).where(IpAllowRule.enabled.is_(True)))).all()
    networks: list[tuple[str | None, object]] = []
    for rule in rules:
        try:
            networks.append((rule.id, parse_network(rule.cidr)))
        except ValueError:
            # Stored rows are normalized on the way in, so this only happens if one was edited
            # by hand. Skip it rather than failing the refresh and freezing the whole snapshot.
            logger.warning("Ignoring unparseable allow rule %s: %s", rule.id, rule.cidr)
    networks.extend((None, network) for network in bootstrap_networks())

    _snapshot = Snapshot(
        mode=(policy.ip_allowlist_mode if policy else "disabled") or "disabled",
        networks=tuple(networks),
        confirm_by=_aware(policy.ip_allowlist_confirm_by) if policy else None,
    )
    _loaded = True
    return _snapshot


def is_exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS


def classify(path: str) -> str:
    if path.startswith(_AUTH_PREFIXES):
        return "sign-in"
    return "api" if path.startswith("/api/") else "ui"


@dataclass
class Decision:
    allowed: bool
    #: True when the request only *would* have been blocked, i.e. audit mode.
    audit_only: bool = False
    reason: str = ""
    matched_rule_id: str | None = None


def evaluate(request) -> Decision:
    """Should this request be served? Reads only in-memory state."""
    settings = get_settings()
    if settings.ip_allowlist_disabled:
        return Decision(allowed=True, reason="kill switch")
    if not _loaded or not _snapshot.active:
        return Decision(allowed=True, reason="inactive")
    path = request.url.path
    if is_exempt(path):
        return Decision(allowed=True, reason="exempt")

    address = parse_address(client_ip(request))
    for rule_id, network in _snapshot.networks:
        if address is not None and address.version == network.version and address in network:
            if rule_id:
                _RULE_HITS.add(rule_id)
            return Decision(allowed=True, reason="allowed", matched_rule_id=rule_id)

    audit_only = _snapshot.mode != "enforce"
    _remember_block(client_ip(request) or "unknown", path, audit_only)
    return Decision(allowed=audit_only, audit_only=audit_only, reason="not on the allowlist")


def allows(address_text: str | None, networks) -> bool:
    """Would `networks` admit this address? Backs the lock-out guards on the write routes."""
    return matches(parse_address(address_text), networks)


def compile_rules(rules) -> list:
    """Networks for a hypothetical rule set, plus the bootstrap ranges that always apply."""
    compiled = []
    for rule in rules:
        try:
            compiled.append(parse_network(rule.cidr))
        except ValueError:
            continue
    return compiled + bootstrap_networks()


def _remember_block(ip: str, path: str, audit_only: bool) -> None:
    _BLOCK_BUFFER.append((ip[:64], classify(path), path[:200], audit_only, utcnow()))


async def flush_block_events(db: AsyncSession) -> int:
    """Drain the buffer into coalesced rows. Called by the scheduler, not by the request path."""
    if not _BLOCK_BUFFER:
        return 0
    pending: list[tuple[str, str, str, bool, datetime]] = []
    while _BLOCK_BUFFER:
        pending.append(_BLOCK_BUFFER.popleft())

    now = utcnow()
    written = 0
    grouped: dict[tuple[str, bool], list[tuple[str, str, str, bool, datetime]]] = {}
    for item in pending:
        grouped.setdefault((item[0], item[3]), []).append(item)

    for (ip, audit_only), items in grouped.items():
        row = await db.scalar(
            select(IpBlockEvent)
            .where(IpBlockEvent.ip == ip, IpBlockEvent.audit_only.is_(audit_only))
            .order_by(IpBlockEvent.last_seen_at.desc())
            .limit(1)
        )
        last_seen = _aware(row.last_seen_at) if row else None
        if row is None or last_seen is None or (now - last_seen) > _COALESCE_WINDOW:
            row = IpBlockEvent(
                id=new_id(), ip=ip, path_class=items[-1][1], last_path=items[-1][2],
                hit_count=0, audit_only=audit_only, first_seen_at=items[0][4],
            )
            db.add(row)
        row.hit_count += len(items)
        row.path_class = items[-1][1]
        row.last_path = items[-1][2]
        row.last_seen_at = items[-1][4]
        written += 1

    await db.commit()
    return written


async def prune_block_events(db: AsyncSession) -> int:
    cutoff = utcnow() - timedelta(days=BLOCK_EVENT_RETENTION_DAYS)
    stale = (await db.scalars(select(IpBlockEvent.id).where(IpBlockEvent.last_seen_at < cutoff))).all()
    if stale:
        await db.execute(delete(IpBlockEvent).where(IpBlockEvent.id.in_(stale)))
        await db.commit()
    return len(stale)


async def expire_commit_confirm(db: AsyncSession) -> bool:
    """Revert unconfirmed enforcement to audit once the window closes.

    This is the recovery path that needs nothing but patience: enable enforcement, discover you
    cannot reach the app, wait, and the app lets you back in by itself.
    """
    policy = await db.scalar(select(SecurityPolicy).where(SecurityPolicy.id == 1))
    if not policy or policy.ip_allowlist_mode != "enforce":
        return False
    deadline = _aware(policy.ip_allowlist_confirm_by)
    if deadline is None or deadline > utcnow():
        return False
    policy.ip_allowlist_mode = "audit"
    policy.ip_allowlist_confirm_by = None
    await db.commit()
    logger.warning("IP allowlist enforcement was not confirmed in time; reverted to audit mode")
    await refresh(db)
    return True


async def maintain(db: AsyncSession) -> None:
    """Everything the scheduler's poll cycle owes this module."""
    global _last_prune
    try:
        await expire_commit_confirm(db)
        await flush_block_events(db)
        await _flush_rule_hits(db)
        # Pruning is a table scan, so it runs hourly rather than on every 15-second cycle.
        if _last_prune is None or (utcnow() - _last_prune) > timedelta(hours=1):
            _last_prune = utcnow()
            await prune_block_events(db)
        await refresh(db)
    except Exception:  # a maintenance failure must never stop the scheduler
        logger.exception("IP allowlist maintenance failed")


async def _flush_rule_hits(db: AsyncSession) -> int:
    """Record which rules were used, so a stale range is visible in the UI."""
    if not _RULE_HITS:
        return 0
    hits = set(_RULE_HITS)
    _RULE_HITS.clear()
    now = utcnow()
    rows = (await db.scalars(select(IpAllowRule).where(IpAllowRule.id.in_(hits)))).all()
    for row in rows:
        row.last_seen_at = now
    await db.commit()
    return len(rows)


def describe(snapshot_value: Snapshot | None = None) -> str:
    state = snapshot_value or _snapshot
    if not state.active:
        return "IP access control is off"
    count = len(state.networks)
    verb = "Enforcing" if state.mode == "enforce" else "Auditing"
    return f"{verb} {count} allowed range{'' if count == 1 else 's'}"
