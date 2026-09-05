from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.connections import decrypt_value, encrypt_value
from app.models import Group, LoginSession, Role, Schedule, ScheduleRun, User, VirtualMachine, VmAttempt, new_id
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


async def test_dashboard_endpoints_require_the_dashboard_capability(session) -> None:
    custom = Role(id=new_id(), name="inventory-only", description="", is_system=False,
                  permissions_json=["vms.read"])
    session.add(custom)
    await session.flush()
    user = await make_login(session, "inventory-reader", custom)

    async with signed_in(session, user) as client:
        assert (await client.get("/api/dashboard")).status_code == 403
        assert (await client.get("/api/overview")).status_code == 403


@pytest.mark.parametrize("permission", ["groups.read", "vms.read", "schedules.read", "imports.write", "connections.manage"])
async def test_features_that_need_public_connection_metadata_can_list_it(session, permission: str) -> None:
    custom = Role(id=new_id(), name=f"only-{permission}", description="", is_system=False,
                  permissions_json=[permission])
    session.add(custom)
    await session.flush()
    user = await make_login(session, f"reader-{permission.replace('.', '-')}", custom)

    async with signed_in(session, user) as client:
        response = await client.get("/api/connections")

    assert response.status_code == 200, response.text


async def test_unrelated_capability_cannot_list_connection_metadata(session) -> None:
    custom = Role(id=new_id(), name="notifications-only", description="", is_system=False,
                  permissions_json=["notifications.read"])
    session.add(custom)
    await session.flush()
    user = await make_login(session, "notification-reader", custom)

    async with signed_in(session, user) as client:
        response = await client.get("/api/connections")

    assert response.status_code == 403


async def test_group_read_does_not_disclose_vm_or_schedule_details(session) -> None:
    group = Group(id=new_id(), name="Restricted", path="", depth=0)
    group.path = f"/{group.id}/"
    vm = VirtualMachine(
        id=new_id(), group_id=group.id, vm_resource_id="/subscriptions/s/resourceGroups/r/providers/Microsoft.Compute/virtualMachines/secret-vm",
        normalized_resource_id="/subscriptions/s/resourcegroups/r/providers/microsoft.compute/virtualmachines/secret-vm",
        display_name="secret-vm", vm_name="secret-vm",
    )
    schedule = Schedule(id=new_id(), name="Secret wave", schedule_type="daily", start_time="07:00", timezone="UTC", target_type="group", target_id=group.id)
    role = Role(id=new_id(), name="group-reader-only", description="", is_system=False, permissions_json=["groups.read"])
    session.add_all([group, vm, schedule, role])
    await session.commit()
    reader = await make_login(session, "group-reader-only", role)

    async with signed_in(session, reader) as client:
        listed = await client.get("/api/groups")
        detail = await client.get(f"/api/groups/{group.id}")

    assert listed.status_code == detail.status_code == 200
    assert listed.json()[0]["subtree_vm_count"] == 0
    assert listed.json()[0]["subtree_schedule_count"] == 0
    assert detail.json()["vms"] == []
    assert detail.json()["schedules"] == []


async def test_schedule_read_exposes_count_but_not_vm_or_run_details(session) -> None:
    group = Group(id=new_id(), name="Restricted", path="", depth=0)
    group.path = f"/{group.id}/"
    vm = VirtualMachine(
        id=new_id(), group_id=group.id, vm_resource_id="/subscriptions/s/resourceGroups/r/providers/Microsoft.Compute/virtualMachines/secret-vm",
        normalized_resource_id="/subscriptions/s/resourcegroups/r/providers/microsoft.compute/virtualmachines/secret-vm",
        display_name="secret-vm", vm_name="secret-vm",
    )
    schedule = Schedule(id=new_id(), name="Visible wave", schedule_type="daily", start_time="07:00", timezone="UTC", target_type="group", target_id=group.id)
    run = ScheduleRun(id=new_id(), schedule_id=schedule.id, schedule_name=schedule.name)
    attempt = VmAttempt(id=new_id(), schedule_id=schedule.id, run_id=run.id, vm_id=vm.id, vm_resource_id=vm.vm_resource_id)
    role = Role(id=new_id(), name="schedule-reader-only", description="", is_system=False, permissions_json=["schedules.read"])
    session.add_all([group, vm, schedule, run, attempt, role])
    await session.commit()
    reader = await make_login(session, "schedule-reader-only", role)

    async with signed_in(session, reader) as client:
        response = await client.get(f"/api/schedules/{schedule.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["schedule"]["vm_count"] == 1
    assert body["vms"] == []
    assert body["attempts"] == []
    assert body["runs"] == []


async def test_group_writer_cannot_cascade_delete_hidden_vms_or_schedules(session) -> None:
    group = Group(id=new_id(), name="Protected", path="", depth=0)
    group.path = f"/{group.id}/"
    vm = VirtualMachine(
        id=new_id(), group_id=group.id, vm_resource_id="/subscriptions/s/resourceGroups/r/providers/Microsoft.Compute/virtualMachines/vm",
        normalized_resource_id="/subscriptions/s/resourcegroups/r/providers/microsoft.compute/virtualmachines/vm", vm_name="vm",
    )
    schedule = Schedule(id=new_id(), name="Protected wave", schedule_type="daily", start_time="07:00", timezone="UTC", target_type="group", target_id=group.id)
    role = Role(id=new_id(), name="group-writer-only", description="", is_system=False, permissions_json=["groups.read", "groups.write"])
    session.add_all([group, vm, schedule, role])
    await session.commit()
    writer = await make_login(session, "group-writer-only", role)

    async with signed_in(session, writer) as client:
        response = await client.delete(f"/api/groups/{group.id}")

    assert response.status_code == 403
    assert "vms.write" in response.text
    assert await session.get(Group, group.id) is not None


async def test_vm_writer_cannot_delete_a_direct_schedule(session) -> None:
    group = Group(id=new_id(), name="Protected", path="", depth=0)
    group.path = f"/{group.id}/"
    vm = VirtualMachine(
        id=new_id(), group_id=group.id, vm_resource_id="/subscriptions/s/resourceGroups/r/providers/Microsoft.Compute/virtualMachines/vm",
        normalized_resource_id="/subscriptions/s/resourcegroups/r/providers/microsoft.compute/virtualmachines/vm", vm_name="vm",
    )
    schedule = Schedule(id=new_id(), name="Protected wave", schedule_type="daily", start_time="07:00", timezone="UTC", target_type="vm", target_id=vm.id)
    role = Role(id=new_id(), name="vm-writer-only", description="", is_system=False, permissions_json=["vms.read", "vms.write"])
    session.add_all([group, vm, schedule, role])
    await session.commit()
    writer = await make_login(session, "vm-writer-only", role)

    async with signed_in(session, writer) as client:
        response = await client.delete(f"/api/vms/{vm.id}")

    assert response.status_code == 403
    assert "schedules.write" in response.text
    assert await session.get(VirtualMachine, vm.id) is not None


async def test_connection_discovery_is_a_csrf_protected_post(session, monkeypatch) -> None:
    roles = await seeded(session)
    admin = await make_login(session, "discovery-admin", roles["admin"])

    async def fake_discovery(connection_id, action, user, db):
        return {"ok": True, "subscriptions": [], "connection_id": connection_id, "action": action}

    monkeypatch.setattr("app.main.connection_live_action", fake_discovery)
    async with signed_in(session, admin) as client:
        get_response = await client.get("/api/connections/connection-1/discover")
        post_response = await client.post("/api/connections/connection-1/discover")

    assert get_response.status_code == 405
    assert post_response.status_code == 200
    assert post_response.json()["action"] == "discovered"


async def test_live_vm_discovery_is_not_a_side_effectful_get(session) -> None:
    roles = await seeded(session)
    user = await make_login(session, "vm-discovery-user", roles["viewer"])

    async with signed_in(session, user) as client:
        response = await client.get(
            "/api/connections/connection-1/vms",
            params={"subscription_id": "12345678-1234-1234-1234-123456789abc"},
        )

    assert response.status_code == 405


async def test_login_response_contains_current_custom_role_permissions(session) -> None:
    role = Role(id=new_id(), name="dashboard-login", description="", is_system=False, permissions_json=["dashboard.read"])
    session.add(role)
    await session.flush()
    user = await make_login(session, "dashboard-login", role)

    from app.database import get_db
    from app.main import app
    import httpx

    app.dependency_overrides[get_db] = lambda: session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/api/auth/login", json={"username": user.username, "password": _PASSWORD})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["user"]["permissions"] == ["dashboard.read"]


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

