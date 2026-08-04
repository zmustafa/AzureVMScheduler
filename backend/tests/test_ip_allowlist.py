"""The IP allowlist: address resolution, the enforcement decision, and the lock-out guards.

The threat these cover is not "does the happy path work" but "can this be bypassed, and can it
brick the deployment". Both failure modes are worse than the exposure the feature removes.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import firewall
from app.config import get_settings
from app.models import IpAllowRule, IpBlockEvent, SecurityPolicy, new_id, utcnow
from app.netaddr import client_ip, parse_network

from test_access_control import seeded
from test_identity import make_login, signed_in


def request_for(path: str = "/api/auth/login", *, peer: str = "203.0.113.9", forwarded: str | None = None):
    headers = {"x-forwarded-for": forwarded} if forwarded else {}
    return SimpleNamespace(url=SimpleNamespace(path=path), headers=headers, client=SimpleNamespace(host=peer))


@pytest.fixture
def trusted_proxy():
    """Behave as though one trusted reverse proxy (Container Apps ingress) sits in front."""
    settings = get_settings()
    before = (settings.trust_forwarded_headers, settings.forwarded_hops)
    settings.trust_forwarded_headers, settings.forwarded_hops = True, 1
    yield settings
    settings.trust_forwarded_headers, settings.forwarded_hops = before


# -- who is calling ------------------------------------------------------


def test_the_peer_address_is_used_when_forwarded_headers_are_not_trusted() -> None:
    settings = get_settings()
    before = settings.trust_forwarded_headers
    settings.trust_forwarded_headers = False
    try:
        assert client_ip(request_for(forwarded="198.51.100.7")) == "203.0.113.9"
    finally:
        settings.trust_forwarded_headers = before


def test_a_client_cannot_forge_its_address_by_prepending_to_the_forwarded_chain(trusted_proxy) -> None:
    """The classic bypass: read the leftmost X-Forwarded-For entry and trust it.

    Every proxy *appends*, so with one trusted hop the only vouched-for entry is the last one.
    A client that sends its own header simply shifts its real address one place right.
    """
    forged = client_ip(request_for(forwarded="10.0.0.1, 198.51.100.7"))
    assert forged == "198.51.100.7"
    assert forged != "10.0.0.1"


def test_more_proxies_means_counting_further_from_the_right(trusted_proxy) -> None:
    trusted_proxy.forwarded_hops = 2
    assert client_ip(request_for(forwarded="10.0.0.1, 198.51.100.7, 172.16.0.1")) == "198.51.100.7"


def test_a_short_forwarded_chain_falls_back_to_the_peer(trusted_proxy) -> None:
    """Fewer entries than trusted hops means the header did not come through the expected path."""
    trusted_proxy.forwarded_hops = 3
    assert client_ip(request_for(forwarded="198.51.100.7")) == "203.0.113.9"


def test_an_unparseable_forwarded_value_falls_back_to_the_peer(trusted_proxy) -> None:
    assert client_ip(request_for(forwarded="not-an-address")) == "203.0.113.9"


def test_a_forwarded_entry_may_carry_a_port(trusted_proxy) -> None:
    assert client_ip(request_for(forwarded="198.51.100.7:44321")) == "198.51.100.7"
    assert client_ip(request_for(forwarded="[2001:db8::5]:44321")) == "2001:db8::5"


# -- rule normalization --------------------------------------------------


@pytest.mark.parametrize(
    ("entered", "stored"),
    [
        ("203.0.113.4", "203.0.113.4/32"),          # a bare address is a single host
        ("2001:db8::5", "2001:db8::5/128"),         # ...and so is a bare IPv6 address
        (" 10.0.0.0/8 ", "10.0.0.0/8"),
        ("10.0.0.5/24", "10.0.0.0/24"),             # host bits are dropped, never rejected
        ("2001:db8::/32", "2001:db8::/32"),
    ],
)
def test_rules_are_stored_normalized(entered: str, stored: str) -> None:
    """A rule must never read differently from how it behaves."""
    assert str(parse_network(entered)) == stored


def test_an_empty_rule_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_network("  ")


def test_an_ipv4_address_is_never_inside_an_ipv6_range() -> None:
    assert firewall.allows("203.0.113.4", [parse_network("::/0")]) is False
    assert firewall.allows("2001:db8::5", [parse_network("2001:db8::/32")]) is True


# -- the decision --------------------------------------------------------


def snapshot_with(mode: str, cidrs: list[str]) -> None:
    firewall._snapshot = firewall.Snapshot(
        mode=mode,
        networks=tuple((f"rule-{index}", parse_network(cidr)) for index, cidr in enumerate(cidrs)),
    )
    firewall._loaded = True


def test_disabled_mode_allows_everything() -> None:
    snapshot_with("disabled", ["203.0.113.0/24"])
    assert firewall.evaluate(request_for(peer="198.51.100.1")).allowed is True


def test_enforce_refuses_an_address_that_is_not_listed() -> None:
    snapshot_with("enforce", ["203.0.113.0/24"])
    decision = firewall.evaluate(request_for(peer="198.51.100.1"))
    assert decision.allowed is False
    assert decision.audit_only is False


def test_enforce_admits_a_listed_address() -> None:
    snapshot_with("enforce", ["203.0.113.0/24"])
    decision = firewall.evaluate(request_for(peer="203.0.113.9"))
    assert decision.allowed is True
    assert decision.matched_rule_id == "rule-0"


def test_audit_mode_records_without_refusing() -> None:
    snapshot_with("audit", ["203.0.113.0/24"])
    decision = firewall.evaluate(request_for(peer="198.51.100.1"))
    assert decision.allowed is True
    assert decision.audit_only is True
    assert len(firewall._BLOCK_BUFFER) == 1


def test_an_empty_rule_set_fails_open() -> None:
    """Deleting the last rule is always an accident; blocking the world would brick the app."""
    snapshot_with("enforce", [])
    assert firewall.snapshot().active is False
    assert firewall.evaluate(request_for(peer="198.51.100.1")).allowed is True


def test_a_snapshot_that_was_never_loaded_fails_open() -> None:
    firewall.reset_for_tests()
    assert firewall.evaluate(request_for(peer="198.51.100.1")).allowed is True


def test_the_environment_kill_switch_overrides_everything() -> None:
    snapshot_with("enforce", ["203.0.113.0/24"])
    settings = get_settings()
    before = settings.ip_allowlist_disabled
    settings.ip_allowlist_disabled = True
    try:
        assert firewall.evaluate(request_for(peer="198.51.100.1")).allowed is True
    finally:
        settings.ip_allowlist_disabled = before


def test_bootstrap_ranges_are_always_allowed() -> None:
    settings = get_settings()
    before = settings.ip_allowlist_bootstrap
    settings.ip_allowlist_bootstrap = "198.51.100.0/24, nonsense"
    try:
        networks = firewall.bootstrap_networks()
        assert [str(item) for item in networks] == ["198.51.100.0/24"]
        assert firewall.allows("198.51.100.7", networks) is True
    finally:
        settings.ip_allowlist_bootstrap = before


# -- everywhere or nowhere ------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/login",
        "/api/auth/change-password",
        "/api/auth/oidc/abc/start",
        "/api/auth/saml/abc/acs",
        "/api/vms",
        "/settings/access",
        "/",
    ],
)
def test_enforcement_covers_every_path(path: str) -> None:
    """There is no partial coverage. "The firewall is on" has to mean one thing."""
    snapshot_with("enforce", ["203.0.113.0/24"])
    assert firewall.evaluate(request_for(path, peer="198.51.100.1")).allowed is False


@pytest.mark.parametrize(
    ("path", "label"),
    [
        ("/api/auth/login", "sign-in"),
        ("/api/vms", "api"),
        ("/settings/access", "ui"),
    ],
)
def test_a_blocked_request_is_labelled_by_what_it_reached_for(path: str, label: str) -> None:
    """Labels only describe the refusal; they never decide it. "Someone is guessing passwords"
    should read differently from "someone loaded a page"."""
    assert firewall.classify(path) == label


@pytest.mark.parametrize("path", ["/api/health", "/health", "/healthz", "/readyz"])
def test_health_probes_are_the_only_exemption(path: str) -> None:
    """Container Apps probes from platform addresses nobody would allowlist; a failing probe
    restarts the container forever, which would turn a typo into an outage loop."""
    snapshot_with("enforce", ["203.0.113.0/24"])
    assert firewall.evaluate(request_for(path, peer="198.51.100.1")).allowed is True


# -- block events --------------------------------------------------------


async def test_repeat_offenders_are_coalesced_into_one_row(session) -> None:
    """A blocked request must never cost a database write of its own: the attacker chooses how
    many they send, so a row per request is an amplification primitive."""
    snapshot_with("enforce", ["203.0.113.0/24"])
    for _ in range(25):
        firewall.evaluate(request_for(peer="198.51.100.1"))
    await firewall.flush_block_events(session)

    from sqlalchemy import select

    events = (await session.scalars(select(IpBlockEvent))).all()
    assert len(events) == 1
    assert events[0].hit_count == 25
    assert events[0].ip == "198.51.100.1"
    assert events[0].audit_only is False


async def test_stale_block_events_are_pruned(session) -> None:
    session.add(IpBlockEvent(id=new_id(), ip="198.51.100.1", hit_count=3, last_seen_at=utcnow() - timedelta(days=30), first_seen_at=utcnow() - timedelta(days=30)))
    await session.commit()
    assert await firewall.prune_block_events(session) == 1


# -- commit-confirm ------------------------------------------------------


async def test_unconfirmed_enforcement_reverts_to_audit(session) -> None:
    """The recovery path that needs no Azure access: enable it, lose access, wait, get back in."""
    session.add(SecurityPolicy(id=1, ip_allowlist_mode="enforce", ip_allowlist_confirm_by=utcnow() - timedelta(minutes=1)))
    session.add(IpAllowRule(id=new_id(), cidr="203.0.113.0/24", enabled=True))
    await session.commit()

    assert await firewall.expire_commit_confirm(session) is True
    policy = await session.get(SecurityPolicy, 1)
    assert policy.ip_allowlist_mode == "audit"
    assert policy.ip_allowlist_confirm_by is None
    assert firewall.snapshot().mode == "audit"


async def test_enforcement_stands_while_the_window_is_open(session) -> None:
    session.add(SecurityPolicy(id=1, ip_allowlist_mode="enforce", ip_allowlist_confirm_by=utcnow() + timedelta(minutes=10)))
    await session.commit()
    assert await firewall.expire_commit_confirm(session) is False
    assert (await session.get(SecurityPolicy, 1)).ip_allowlist_mode == "enforce"


async def test_a_confirmed_policy_is_never_reverted(session) -> None:
    session.add(SecurityPolicy(id=1, ip_allowlist_mode="enforce", ip_allowlist_confirm_by=None))
    await session.commit()
    assert await firewall.expire_commit_confirm(session) is False


# -- the API -------------------------------------------------------------


async def test_a_rule_is_stored_normalized_and_compiled_immediately(session) -> None:
    """The snapshot lives in memory, so a write that does not refresh it would be a no-op until
    the next scheduler tick — a security control that silently lags is not a control."""
    roles = await seeded(session)
    admin = await make_login(session, "ipadmin", roles["admin"])
    async with signed_in(session, admin) as client:
        response = await client.post("/api/access/ip-rules", json={"cidr": "10.0.0.5/24", "label": "Office"})
        assert response.status_code == 201, response.text
        assert response.json()["cidr"] == "10.0.0.0/24"
    assert [str(network) for _, network in firewall.snapshot().networks] == ["10.0.0.0/24"]


async def test_the_whole_internet_needs_a_second_confirmation(session) -> None:
    roles = await seeded(session)
    admin = await make_login(session, "ipadmin", roles["admin"])
    async with signed_in(session, admin) as client:
        refused = await client.post("/api/access/ip-rules", json={"cidr": "0.0.0.0/0"})
        assert refused.status_code == 422
        accepted = await client.post("/api/access/ip-rules", json={"cidr": "0.0.0.0/0", "allow_any": True})
        assert accepted.status_code == 201


async def test_duplicate_rules_are_refused(session) -> None:
    roles = await seeded(session)
    admin = await make_login(session, "ipadmin", roles["admin"])
    async with signed_in(session, admin) as client:
        assert (await client.post("/api/access/ip-rules", json={"cidr": "10.0.0.0/24"})).status_code == 201
        # The same range written differently must still collide, which is why storage is normalized.
        clash = await client.post("/api/access/ip-rules", json={"cidr": "10.0.0.7/24"})
        assert clash.status_code == 409


async def test_enforcing_from_an_unlisted_address_is_refused(session) -> None:
    """The guard that makes this feature safe to own: you cannot switch it on from outside it."""
    roles = await seeded(session)
    admin = await make_login(session, "ipadmin", roles["admin"])
    async with signed_in(session, admin) as client:
        await client.post("/api/access/ip-rules", json={"cidr": "203.0.113.0/24"})
        response = await client.put("/api/access/ip-policy", json={"mode": "enforce"})
        assert response.status_code == 409
        assert "lock you out" in response.json()["detail"]


async def test_enforcing_with_no_rules_at_all_is_refused(session) -> None:
    roles = await seeded(session)
    admin = await make_login(session, "ipadmin", roles["admin"])
    async with signed_in(session, admin) as client:
        response = await client.put("/api/access/ip-policy", json={"mode": "enforce"})
        assert response.status_code == 409


async def test_enforcing_from_a_listed_address_arms_the_safety_timer(session) -> None:
    roles = await seeded(session)
    admin = await make_login(session, "ipadmin", roles["admin"])
    async with signed_in(session, admin) as client:
        # The test transport calls from 127.0.0.1, which is this caller's real address.
        await client.post("/api/access/ip-rules", json={"cidr": "127.0.0.1", "label": "Me"})
        response = await client.put("/api/access/ip-policy", json={"mode": "enforce", "confirm_minutes": 15})
        assert response.status_code == 200, response.text
        assert response.json()["confirm_by"] is not None
        assert response.json()["your_ip_allowed"] is True

        confirmed = await client.post("/api/access/ip-policy/confirm")
        assert confirmed.json()["confirm_by"] is None


async def test_deleting_the_rule_that_admits_you_is_refused_while_enforcing(session) -> None:
    roles = await seeded(session)
    admin = await make_login(session, "ipadmin", roles["admin"])
    async with signed_in(session, admin) as client:
        created = await client.post("/api/access/ip-rules", json={"cidr": "127.0.0.1", "label": "Me"})
        rule_id = created.json()["id"]
        # A second range means removing the first genuinely locks the caller out, rather than
        # emptying the list — which fails open and is handled separately below.
        await client.post("/api/access/ip-rules", json={"cidr": "203.0.113.0/24"})
        await client.put("/api/access/ip-policy", json={"mode": "enforce"})

        refused = await client.delete(f"/api/access/ip-rules/{rule_id}")
        assert refused.status_code == 409

        # Disabling it is the same lock-out by another name, so it is refused too.
        disabled = await client.patch(f"/api/access/ip-rules/{rule_id}", json={"enabled": False})
        assert disabled.status_code == 409


async def test_emptying_the_list_turns_enforcement_off_rather_than_pretending(session) -> None:
    """An empty allowlist fails open, so leaving the mode at `enforce` would show a green
    "Enforcing" banner over a door that is wide open."""
    roles = await seeded(session)
    admin = await make_login(session, "ipadmin", roles["admin"])
    async with signed_in(session, admin) as client:
        created = await client.post("/api/access/ip-rules", json={"cidr": "127.0.0.1", "label": "Me"})
        await client.put("/api/access/ip-policy", json={"mode": "enforce"})

        assert (await client.delete(f"/api/access/ip-rules/{created.json()['id']}")).status_code == 200
        state = (await client.get("/api/access/ip-rules")).json()
        assert state["mode"] == "disabled"
        assert state["rules"] == []


async def test_a_second_rule_may_be_removed_freely(session) -> None:
    roles = await seeded(session)
    admin = await make_login(session, "ipadmin", roles["admin"])
    async with signed_in(session, admin) as client:
        await client.post("/api/access/ip-rules", json={"cidr": "127.0.0.1"})
        spare = await client.post("/api/access/ip-rules", json={"cidr": "203.0.113.0/24"})
        await client.put("/api/access/ip-policy", json={"mode": "enforce"})
        assert (await client.delete(f"/api/access/ip-rules/{spare.json()['id']}")).status_code == 200


async def test_the_middleware_refuses_an_unlisted_caller_end_to_end(session) -> None:
    """Everything above tests the parts; this proves the parts are actually wired to the door."""
    roles = await seeded(session)
    admin = await make_login(session, "ipadmin", roles["admin"])
    async with signed_in(session, admin) as client:
        await client.post("/api/access/ip-rules", json={"cidr": "203.0.113.0/24", "label": "Somewhere else"})
        await client.post("/api/access/ip-rules", json={"cidr": "127.0.0.1", "label": "Me"})
        await client.put("/api/access/ip-policy", json={"mode": "enforce"})

        # Still admitted: the caller is 127.0.0.1 and that is on the list.
        assert (await client.get("/api/access/ip-rules")).status_code == 200

        # Now take the caller off the list from underneath, as the auto-revert or another
        # administrator could, and confirm the door is genuinely shut.
        snapshot_with("enforce", ["203.0.113.0/24"])
        blocked = await client.get("/api/access/ip-rules")
        assert blocked.status_code == 403
        assert blocked.json() == {"detail": "Forbidden"}


async def test_my_ip_reports_what_the_server_resolved(session) -> None:
    roles = await seeded(session)
    admin = await make_login(session, "ipadmin", roles["admin"])
    async with signed_in(session, admin) as client:
        response = await client.get("/api/access/ip-rules/my-ip")
        assert response.status_code == 200
        assert response.json()["ip"] == "127.0.0.1"
        assert response.json()["forwarded_hops"] >= 1


async def test_the_ip_surface_needs_the_manage_capability(session) -> None:
    roles = await seeded(session)
    viewer = await make_login(session, "ipviewer", roles["viewer"])
    async with signed_in(session, viewer) as client:
        assert (await client.get("/api/access/ip-rules")).status_code == 403
        assert (await client.post("/api/access/ip-rules", json={"cidr": "10.0.0.0/8"})).status_code == 403
        assert (await client.put("/api/access/ip-policy", json={"mode": "audit"})).status_code == 403
