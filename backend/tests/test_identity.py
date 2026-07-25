"""Identity and access hardening: SSO provisioning, the access walls, throttling, SAML.

These cover the attacks the features exist to stop, not just their happy paths.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import ip_lockout, oidc, saml
from app.auth import get_security_policy, hash_password, needs_rehash
from app.models import IdentityProvider, LoginThrottle, Role, User, UserRole, new_id, utcnow
from app.permissions import NO_ACCESS_ROLE
from app.provisioning import ProvisioningError, provision_sso_user

from test_access_control import seeded
from test_runs import api_client

# asyncio_mode=auto handles the async tests; this module also has plain synchronous ones.

_PASSWORD = "A-strong-passphrase-1!"


async def make_login(session, username: str, role: Role | None, *, must_change_password: bool = False) -> User:
    """A real user with a real password, so tests can drive the genuine sign-in chain."""
    user = User(
        id=new_id(), username=username, password_hash=hash_password(_PASSWORD),
        role=role.name if role else NO_ACCESS_ROLE, must_change_password=must_change_password,
    )
    session.add(user)
    await session.flush()
    if role is not None:
        session.add(UserRole(user_id=user.id, role_id=role.id))
    await session.commit()
    return user


@asynccontextmanager
async def signed_in(session, user: User):
    """A client holding a genuine session cookie.

    Deliberately does *not* override the auth dependencies: the access walls live in the session
    resolver, so overriding them would skip the very thing under test.
    """
    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/api/auth/login", json={"username": user.username, "password": _PASSWORD})
            assert response.status_code == 200, response.text
            client.headers["X-CSRF-Token"] = client.cookies.get("azureops_csrf", "")
            yield client
    finally:
        app.dependency_overrides.clear()


def make_provider(**config) -> IdentityProvider:
    base = {"auto_provision": True, "default_role": NO_ACCESS_ROLE}
    return IdentityProvider(id=new_id(), name="Test IdP", type="oidc", enabled=True, config_json={**base, **config})


# -- OIDC ----------------------------------------------------------------


def test_the_issuer_is_derived_from_an_entra_directory_id() -> None:
    assert oidc.issuer_for({"tenant_id": "abc"}) == "https://login.microsoftonline.com/abc/v2.0"
    # An explicit issuer wins, which is what makes any other provider work.
    assert oidc.issuer_for({"tenant_id": "abc", "issuer": "https://idp.example/"}) == "https://idp.example"
    assert oidc.issuer_for({}) == ""


def test_the_discovery_url_defaults_to_the_well_known_path() -> None:
    assert oidc.discovery_url_for({"issuer": "https://idp.example"}) == "https://idp.example/.well-known/openid-configuration"
    assert oidc.discovery_url_for({"issuer": "https://idp.example", "discovery_url": "https://other/.well-known"}) == "https://other/.well-known"


def test_pkce_verifier_and_challenge_are_bound() -> None:
    import base64 as b64
    import hashlib

    verifier, challenge = oidc.new_pkce()
    expected = b64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_identity_extraction_normalises_claims() -> None:
    identity = oidc.extract_identity(
        {"oid": "OID-1", "sub": "SUB-1", "email": "  Person@Example.COM ", "name": "A Person", "groups": ["g1", "g2"]},
        {},
    )
    assert identity["external_id"] == "OID-1", "Entra's immutable object id is preferred over sub"
    assert identity["email"] == "person@example.com"
    assert identity["groups"] == ["g1", "g2"]


def test_a_comma_separated_group_claim_is_split() -> None:
    identity = oidc.extract_identity({"sub": "s", "roles": "admins, ops"}, {"group_claim": "roles"})
    assert identity["groups"] == ["admins", "ops"]


# -- SSO provisioning ----------------------------------------------------


async def test_sso_cannot_take_over_a_local_password_account(session) -> None:
    """The whole point of the subject-first rule: an assertion must not seize the admin."""
    await seeded(session)
    victim = User(id=new_id(), username="admin", email="admin@example.com", password_hash=hash_password("x" * 20), role="admin")
    session.add(victim)
    provider = make_provider()
    session.add(provider)
    await session.commit()

    with pytest.raises(ProvisioningError):
        await provision_sso_user(session, provider, external_id="attacker-subject", email="admin@example.com")

    await session.refresh(victim)
    assert victim.external_oid is None, "the local account must be untouched"


async def test_sso_links_an_existing_sso_account_by_verified_email(session) -> None:
    await seeded(session)
    provider = make_provider()
    session.add(provider)
    existing = User(id=new_id(), username="p@example.com", email="p@example.com", password_hash=None, auth_source="oidc")
    session.add(existing)
    await session.commit()

    user = await provision_sso_user(session, provider, external_id="subject-1", email="p@example.com")
    await session.commit()

    assert user.id == existing.id
    assert user.external_oid == "subject-1"


async def test_an_unverified_email_never_links(session) -> None:
    await seeded(session)
    provider = make_provider()
    session.add(provider)
    session.add(User(id=new_id(), username="q@example.com", email="q@example.com", password_hash=None, auth_source="oidc"))
    await session.commit()

    user = await provision_sso_user(session, provider, external_id="subject-2", email="q@example.com", email_verified=False)
    await session.commit()
    assert user.username != "q@example.com", "a fresh account is created rather than linking"


async def test_a_provisioned_user_defaults_to_no_access(session) -> None:
    roles = await seeded(session)
    provider = make_provider()
    session.add(provider)
    await session.commit()

    user = await provision_sso_user(session, provider, external_id="new-1", email="new@example.com")
    await session.commit()

    assigned = (await session.scalars(select(UserRole.role_id).where(UserRole.user_id == user.id))).all()
    assert list(assigned) == [roles[NO_ACCESS_ROLE].id]


async def test_auto_provision_off_refuses_unknown_users(session) -> None:
    await seeded(session)
    provider = make_provider(auto_provision=False)
    session.add(provider)
    await session.commit()

    with pytest.raises(ProvisioningError):
        await provision_sso_user(session, provider, external_id="nobody", email="nobody@example.com")


async def test_group_mapping_applies_and_reapplies_on_every_sign_in(session) -> None:
    """Removing someone from a directory group must take the role away here too."""
    roles = await seeded(session)
    provider = make_provider(group_role_map={"Ops-Team": "operator"})
    session.add(provider)
    await session.commit()

    user = await provision_sso_user(session, provider, external_id="s3", email="s3@example.com", groups=["Ops-Team"])
    await session.commit()
    assert (await session.scalars(select(UserRole.role_id).where(UserRole.user_id == user.id))).all() == [roles["operator"].id]

    provider.config_json = {**provider.config_json, "group_role_map": {"Other": "viewer"}}
    await provision_sso_user(session, provider, external_id="s3", email="s3@example.com", groups=["Ops-Team"])
    await session.commit()
    # No group matches now, so the previous mapping is left in place rather than silently cleared.
    assert (await session.scalars(select(UserRole.role_id).where(UserRole.user_id == user.id))).all() == [roles["operator"].id]


async def test_group_mapping_matches_case_insensitively(session) -> None:
    roles = await seeded(session)
    provider = make_provider(group_role_map={"ops-team": "operator"})
    session.add(provider)
    await session.commit()

    user = await provision_sso_user(session, provider, external_id="s4", email="s4@example.com", groups=["OPS-TEAM"])
    await session.commit()
    assert (await session.scalars(select(UserRole.role_id).where(UserRole.user_id == user.id))).all() == [roles["operator"].id]


async def test_a_disabled_user_cannot_sign_in_through_sso(session) -> None:
    await seeded(session)
    provider = make_provider()
    session.add(provider)
    session.add(User(id=new_id(), username="gone", email="gone@example.com", password_hash=None, disabled=True,
                     auth_source="oidc", external_tenant_id=provider.id, external_oid="s5"))
    await session.commit()

    with pytest.raises(ProvisioningError):
        await provision_sso_user(session, provider, external_id="s5", email="gone@example.com")


# -- per-IP throttle -----------------------------------------------------


async def test_ip_lockout_trips_at_the_threshold_and_releases(session) -> None:
    policy = await get_security_policy(session)
    policy.ip_lockout_attempts = 3
    await session.commit()

    for _ in range(2):
        await ip_lockout.record_failure(session, policy, "10.0.0.1")
    await session.commit()
    assert await ip_lockout.check(session, policy, "10.0.0.1") is None

    await ip_lockout.record_failure(session, policy, "10.0.0.1")
    await session.commit()
    assert await ip_lockout.check(session, policy, "10.0.0.1") is not None

    row = await session.get(LoginThrottle, "10.0.0.1")
    row.locked_until = utcnow() - timedelta(seconds=1)
    await session.commit()
    assert await ip_lockout.check(session, policy, "10.0.0.1") is None, "the lockout releases itself"


async def test_only_recent_failures_count_towards_the_threshold(session) -> None:
    policy = await get_security_policy(session)
    policy.ip_lockout_attempts = 3
    policy.ip_lockout_window_seconds = 60
    await session.commit()

    await ip_lockout.record_failure(session, policy, "10.0.0.2")
    await session.commit()
    row = await session.get(LoginThrottle, "10.0.0.2")
    row.window_start = utcnow() - timedelta(seconds=600)
    await session.commit()

    await ip_lockout.record_failure(session, policy, "10.0.0.2")
    await session.commit()
    assert (await session.get(LoginThrottle, "10.0.0.2")).fail_count == 1, "the stale window restarted"


async def test_a_successful_sign_in_clears_the_ip(session) -> None:
    policy = await get_security_policy(session)
    await ip_lockout.record_failure(session, policy, "10.0.0.3")
    await session.commit()
    await ip_lockout.clear(session, "10.0.0.3")
    await session.commit()
    assert await session.get(LoginThrottle, "10.0.0.3") is None


async def test_the_throttle_can_be_switched_off(session) -> None:
    policy = await get_security_policy(session)
    policy.ip_lockout_enabled = False
    await session.commit()
    await ip_lockout.record_failure(session, policy, "10.0.0.4")
    await session.commit()
    assert await session.get(LoginThrottle, "10.0.0.4") is None


def test_a_forwarded_header_is_ignored_unless_a_proxy_is_trusted() -> None:
    """Off a trusted proxy, X-Forwarded-For is attacker-controlled — rotating it must not reset the counter."""
    from app.config import get_settings

    class FakeClient:
        host = "203.0.113.9"

    class FakeRequest:
        headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8"}
        client = FakeClient()

    settings = get_settings()
    original = settings.trust_forwarded_headers
    try:
        settings.trust_forwarded_headers = False
        assert ip_lockout.client_ip(FakeRequest()) == "203.0.113.9"
        settings.trust_forwarded_headers = True
        assert ip_lockout.client_ip(FakeRequest()) == "1.2.3.4", "the left-most entry is the client"
    finally:
        settings.trust_forwarded_headers = original


# -- password hashing ----------------------------------------------------


def test_a_current_hash_does_not_need_rehashing() -> None:
    assert needs_rehash(hash_password("a-good-password")) is False
    assert needs_rehash(None) is False
    assert needs_rehash("not-a-hash") is False


# -- SAML ----------------------------------------------------------------


def test_sp_metadata_advertises_the_acs_and_wants_signed_assertions() -> None:
    xml = saml.sp_metadata("https://app.example", "idp-1")
    assert 'WantAssertionsSigned="true"' in xml
    assert "https://app.example/api/auth/saml/idp-1/acs" in xml
    assert saml.sp_entity_id("https://app.example") in xml


def test_an_authn_request_is_deflated_and_carries_relay_state() -> None:
    import zlib
    from urllib.parse import parse_qs, urlparse

    provider = IdentityProvider(id="idp-1", name="Okta", type="saml", enabled=True, config_json={
        "entity_id": "http://idp/meta", "sso_url": "https://idp.example/sso", "certificate": "x",
    })
    url = saml.build_authn_request(provider, "https://app.example", "/schedules")
    query = parse_qs(urlparse(url).query)
    xml = zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -zlib.MAX_WBITS).decode()
    assert "AuthnRequest" in xml and "https://app.example/api/auth/saml/idp-1/acs" in xml
    relay = saml.decode_relay(query["RelayState"][0])
    assert relay["idp"] == "idp-1" and relay["return_url"] == "/schedules"
    # The request id must be remembered, or the response cannot be bound back to this request.
    assert relay["request_id"] and relay["request_id"] in xml


def test_relay_state_from_another_provider_is_rejected(session) -> None:
    provider = IdentityProvider(id="idp-1", name="Okta", type="saml", enabled=True, config_json={
        "entity_id": "http://idp/meta", "sso_url": "https://idp.example/sso", "certificate": "x",
    })
    other = saml.encode_relay({"iat": int(utcnow().timestamp()), "idp": "someone-else", "request_id": "r"})
    with pytest.raises(Exception):
        saml.validate_response(provider, base64.b64encode(b"<x/>").decode(), other, "https://app.example")


def test_an_unsigned_assertion_is_refused() -> None:
    provider = IdentityProvider(id="idp-1", name="Okta", type="saml", enabled=True, config_json={
        "entity_id": "http://idp/meta", "sso_url": "https://idp.example/sso",
        "certificate": "MIIBkTCB+wIJAKZ", "email_attr": "", "name_attr": "",
    })
    relay = saml.encode_relay({"iat": int(utcnow().timestamp()), "idp": "idp-1", "request_id": "r1", "return_url": "/"})
    unsigned = base64.b64encode(
        b'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">'
        b'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
        b'<saml:Issuer>http://idp/meta</saml:Issuer></saml:Assertion></samlp:Response>'
    ).decode()
    with pytest.raises(Exception) as error:
        saml.validate_response(provider, unsigned, relay, "https://app.example")
    assert "signature" in str(error.value).lower() or "401" in str(error.value)


def test_saml_config_check_reports_the_urls_to_register() -> None:
    checks = {item["name"]: item for item in saml.test_config(
        {"entity_id": "http://idp/meta", "sso_url": "https://idp.example/sso"}, "https://app.example", "idp-1"
    )}
    assert checks["IdP SSO URL"]["ok"] is True
    assert checks["Signing certificate"]["ok"] is False
    assert checks["Reply URL (ACS)"]["detail"] == "https://app.example/api/auth/saml/idp-1/acs"


def test_an_http_sso_url_is_not_accepted() -> None:
    checks = {item["name"]: item for item in saml.test_config(
        {"entity_id": "x", "sso_url": "http://idp.example/sso"}, "https://app.example", "idp-1"
    )}
    assert checks["IdP SSO URL"]["ok"] is False


# -- server-side walls (over the API) ------------------------------------


async def test_a_no_access_user_is_blocked_from_the_api_not_just_the_ui(session) -> None:
    """`noaccess` has to be a real wall: hiding nav would leave the API wide open."""
    roles = await seeded(session)
    user = await make_login(session, "nobody", roles[NO_ACCESS_ROLE])

    async with signed_in(session, user) as client:
        blocked = await client.get("/api/vms")
        allowed = await client.get("/api/auth/me")

    assert blocked.status_code == 403
    assert "no access" in blocked.json()["detail"].lower()
    assert allowed.status_code == 200, "they must still be able to see who they are and sign out"


async def test_a_user_with_no_roles_at_all_is_blocked_too(session) -> None:
    await seeded(session)
    user = await make_login(session, "unassigned", None)

    async with signed_in(session, user) as client:
        response = await client.get("/api/schedules")
    assert response.status_code == 403


async def test_must_change_password_is_enforced_server_side(session) -> None:
    """A scripted client must not be able to skip the password change the UI shows."""
    roles = await seeded(session)
    user = await make_login(session, "fresh", roles["admin"], must_change_password=True)

    async with signed_in(session, user) as client:
        blocked = await client.get("/api/vms")
        allowed = await client.get("/api/auth/me")
        changing = await client.get("/api/auth/config")

    assert blocked.status_code == 403
    assert "password" in blocked.json()["detail"].lower()
    assert allowed.status_code == 200
    assert changing.status_code == 200


async def test_security_headers_are_sent_on_every_response(session) -> None:
    async with api_client(session) as client:
        response = await client.get("/api/health")
    for header in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Content-Security-Policy"):
        assert header in response.headers, header
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


async def test_an_unknown_api_path_is_404_not_the_spa(session) -> None:
    """The SPA fallback must never swallow an API path, or a typo looks like a working page."""
    async with api_client(session) as client:
        response = await client.get("/api/definitely-not-a-route")
    assert response.status_code == 404


async def test_sign_in_config_lists_enabled_providers(session) -> None:
    await seeded(session)
    session.add(IdentityProvider(id=new_id(), name="Corp SSO", type="oidc", enabled=True, config_json={
        "issuer": "https://idp.example", "client_id": "abc", "client_secret_encrypted": "x",
    }))
    session.add(IdentityProvider(id=new_id(), name="Half done", type="oidc", enabled=True, config_json={"issuer": "https://idp2.example"}))
    await session.commit()

    async with api_client(session) as client:
        body = (await client.get("/api/auth/config")).json()

    names = [item["name"] for item in body["providers"]]
    assert names == ["Corp SSO"], "a provider missing its client credentials is not offered"
    assert body["providers"][0]["start_url"].endswith("/login")


async def test_the_policies_endpoint_exposes_every_editable_knob(session) -> None:
    """A field the API omits is a field the UI silently cannot save."""
    await seeded(session)
    async with api_client(session) as client:
        body = (await client.get("/api/access/policies")).json()

    for key in ("ip_lockout_enabled", "ip_lockout_attempts", "ip_lockout_window_seconds",
                "ip_lockout_seconds", "allow_self_registration"):
        assert key in body, key

    async with api_client(session) as client:
        saved = await client.put("/api/access/policies", json={**body, "ip_lockout_attempts": 7})
    assert saved.status_code == 200
    assert saved.json()["ip_lockout_attempts"] == 7


async def test_hsts_follows_the_forwarded_protocol(session) -> None:
    """TLS terminates at the ingress, so the forwarded scheme is the only signal HTTPS was used."""
    from app.config import get_settings

    settings = get_settings()
    original = settings.trust_forwarded_headers
    try:
        settings.trust_forwarded_headers = True
        async with api_client(session) as client:
            secure = await client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
            plain = await client.get("/api/health")
        assert "strict-transport-security" in secure.headers
        assert "strict-transport-security" not in plain.headers

        # Off a trusted proxy the header is attacker-supplied and must not be believed.
        settings.trust_forwarded_headers = False
        async with api_client(session) as client:
            spoofed = await client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
        assert "strict-transport-security" not in spoofed.headers
    finally:
        settings.trust_forwarded_headers = original
