from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.connections import decrypt_value, encrypt_value
from app.models import Group, LoginSession, Role, User, new_id
from app.oidc import read_state, validate_return_url

from test_access_control import seeded
from test_identity import _PASSWORD, make_login, signed_in


def test_encrypted_oidc_state_round_trip() -> None:
    state = encrypt_value('{"iat":%d,"return_url":"/settings","verifier":"v","nonce":"n"}' % int(datetime.now(timezone.utc).timestamp()))
    assert read_state(state)["return_url"] == "/settings"


def test_rejects_protocol_relative_return_url() -> None:
    with pytest.raises(Exception):
        validate_return_url("//evil.example/path")


def test_secret_encryption_is_not_plaintext() -> None:
    encrypted = encrypt_value("highly-secret")
    assert "highly-secret" not in encrypted
    assert decrypt_value(encrypted) == "highly-secret"

def test_a_passphrase_fernet_key_is_accepted_and_stable() -> None:
    """Deployment templates cannot produce 32 url-safe base64 bytes, so any string must work."""
    from cryptography.fernet import Fernet

    from app import connections
    from app.config import get_settings

    settings = get_settings()
    original = settings.fernet_key
    try:
        settings.fernet_key = "a plain deployment passphrase"
        token = connections._fernet().encrypt(b"secret")
        # A second load of the same passphrase must derive the identical key, or every restart
        # would invalidate the stored Azure credentials.
        assert connections._fernet().decrypt(token) == b"secret"

        # A real Fernet key is still used verbatim rather than derived from.
        raw = Fernet.generate_key().decode()
        settings.fernet_key = raw
        assert connections._fernet().decrypt(Fernet(raw.encode()).encrypt(b"x")) == b"x"
    finally:
        settings.fernet_key = original


# -- cookie transport ----------------------------------------------------


def _request(scheme: str = "http", forwarded: str | None = None):
    headers = {"x-forwarded-proto": forwarded} if forwarded else {}
    return SimpleNamespace(url=SimpleNamespace(scheme=scheme), headers=headers)


@pytest.mark.parametrize(
    ("environment", "trust", "scheme", "forwarded", "expected"),
    [
        ("production", False, "http", None, True),      # explicit production always marks Secure
        ("development", False, "https", None, True),    # direct TLS, whatever the environment says
        ("development", True, "http", "https", True),   # behind a trusted proxy terminating TLS
        ("development", False, "http", "https", False),  # untrusted header must not be believed
        ("development", False, "http", None, False),    # genuine plain HTTP, local development
    ],
)
def test_secure_cookies_follow_the_actual_transport(environment, trust, scheme, forwarded, expected) -> None:
    """Secure must not hinge on ENVIRONMENT alone: a TLS deployment that forgets to set it would
    otherwise hand the browser a session cookie it will replay over plain HTTP."""
    from app.auth import cookies_are_secure
    from app.config import get_settings

    settings = get_settings()
    before = (settings.environment, settings.trust_forwarded_headers)
    try:
        settings.environment, settings.trust_forwarded_headers = environment, trust
        assert cookies_are_secure(_request(scheme, forwarded)) is expected
    finally:
        settings.environment, settings.trust_forwarded_headers = before


# -- login does not leak which usernames exist ---------------------------


async def test_an_unknown_username_still_costs_a_password_verification(session, monkeypatch) -> None:
    """Without the decoy hash the sign-in latency is a username oracle, even though the response
    body is identical for a miss and a wrong password."""
    from app import main

    calls: list[str] = []
    monkeypatch.setattr(main, "verify_dummy_password", lambda password: calls.append(password))

    roles = await seeded(session)
    await make_login(session, "known", roles["viewer"])

    from app.database import get_db
    from app.main import app
    import httpx

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            missing = await client.post("/api/auth/login", json={"username": "ghost", "password": "whatever"})
            wrong = await client.post("/api/auth/login", json={"username": "known", "password": "whatever"})
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json()
    # The decoy ran for the unknown account and not for the real one, so both paths hash once.
    assert calls == ["whatever"]


def test_credential_fields_are_bounded() -> None:
    """An unbounded password on an unauthenticated endpoint is free Argon2 work for an attacker."""
    from pydantic import ValidationError

    from app.schemas import ChangePasswordRequest, LoginRequest

    with pytest.raises(ValidationError):
        LoginRequest(username="a", password="x" * 5000)
    with pytest.raises(ValidationError):
        ChangePasswordRequest(current_password="x" * 5000, new_password="y")


# -- change password is a credential endpoint and is treated as one ------


async def test_change_password_locks_out_after_repeated_wrong_guesses(session) -> None:
    """A stolen session cookie must not turn this into an unthrottled password oracle."""
    roles = await seeded(session)
    user = await make_login(session, "guessed", roles["viewer"])

    async with signed_in(session, user) as client:
        codes = []
        for index in range(8):
            response = await client.post(
                "/api/auth/change-password",
                json={"current_password": f"wrong-{index}", "new_password": "A-new-passphrase-1!"},
            )
            codes.append(response.status_code)
            if response.status_code == 423:
                break

    assert 423 in codes, codes
    assert codes.count(400) <= 5


async def test_changing_a_password_revokes_every_other_session(session) -> None:
    """If the password is being changed because it leaked, the thief's session must not survive."""
    roles = await seeded(session)
    user = await make_login(session, "rotator", roles["viewer"])

    async with signed_in(session, user):
        pass  # the first sign-in leaves a session behind, standing in for the attacker's
    async with signed_in(session, user) as client:
        response = await client.post(
            "/api/auth/change-password",
            json={"current_password": _PASSWORD, "new_password": "A-brand-new-passphrase-1!"},
        )
        assert response.status_code == 200, response.text

    rows = (await session.scalars(select(LoginSession).where(LoginSession.user_id == user.id))).all()
    assert len(rows) == 2
    assert sum(1 for row in rows if row.revoked_at is None) == 1


# -- authorisation uses capabilities, not the cached role string ---------


async def test_admin_only_writes_are_gated_on_capabilities_not_the_role_name(session) -> None:
    """users.role is a display cache that is never recomputed when a role's permissions change.
    Gating on the literal string 'admin' both blocks legitimate delegation and can outlive the
    permissions it was derived from."""
    await seeded(session)
    custom = Role(id=new_id(), name="platform-owner", description="", is_system=False,
                  permissions_json=["*"])
    session.add(custom)
    await session.flush()
    user = await make_login(session, "owner", custom)
    assert user.role == "platform-owner"

    async with signed_in(session, user) as client:
        assert (await client.put("/api/settings/general", json={"default_timezone": "UTC"})).status_code == 200
        assert (await client.get("/api/admin/export")).status_code == 200
        assert (await client.post("/api/admin/reset-estate", json={"confirm": "DELETE"})).status_code == 200


async def test_a_role_named_admin_without_permissions_is_refused(session) -> None:
    """The mirror image: the cached string must not be a grant on its own."""
    roles = await seeded(session)
    roles["admin"].permissions_json = ["dashboard.read"]
    await session.commit()
    user = await make_login(session, "hollow", roles["admin"])
    assert user.role == "admin"

    async with signed_in(session, user) as client:
        assert (await client.put("/api/settings/general", json={"default_timezone": "UTC"})).status_code == 403
        assert (await client.put("/api/connections", json={"display_name": "x", "auth_method": "azure_cli"})).status_code == 403


# -- off-boarding --------------------------------------------------------


async def test_a_user_who_created_things_can_still_be_deleted(session) -> None:
    """groups/vms/schedules carry a nullable created_by pointing at users.id. Deleting without
    releasing them trips the foreign key, so an account that ever created anything could never be
    off-boarded."""
    roles = await seeded(session)
    admin = await make_login(session, "root", roles["admin"])
    leaver = await make_login(session, "leaver", roles["operator"])
    group = Group(id=new_id(), name="owned", parent_id=None, depth=0, path="owned",
                  sequence=1, created_by=leaver.id)
    session.add(group)
    await session.commit()

    async with signed_in(session, admin) as client:
        response = await client.delete(f"/api/access/users/{leaver.id}")

    assert response.status_code == 200, response.text
    assert await session.get(User, leaver.id) is None
    await session.refresh(group)
    assert group.created_by is None

