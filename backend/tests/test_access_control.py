"""Roles, access groups, effective permissions, and the guards that stop a lock-out."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.access import (
    AccessError,
    assert_admin_remains,
    backfill_user_roles,
    effective_permissions,
    migrate_identity_provider,
    seed_system_roles,
    set_user_access_groups,
    set_user_roles,
)
from app.auth import ROLE_PERMISSIONS
from app.models import AccessGroup, IdentityProvider, IdentityProviderSettings, Role, User, UserRole, new_id
from app.permissions import ALL_PERMISSIONS, PERMISSIONS, SYSTEM_ROLES, expand, unknown_permissions

from test_runs import api_client


async def make_user(session, username: str, role: str = "viewer", **kwargs) -> User:
    user = User(id=new_id(), username=username, role=role, **kwargs)
    session.add(user)
    await session.commit()
    return user


async def seeded(session) -> dict[str, Role]:
    await seed_system_roles(session)
    await session.commit()
    return {role.name: role for role in (await session.scalars(select(Role))).all()}


async def seeded_with_admin(session) -> dict[str, Role]:
    """Roles plus a real administrator row.

    The API client signs in as an in-memory user that never reaches the database, so without this
    the lock-out guard correctly sees an estate with no administrator and blocks every change.
    """
    roles = await seeded(session)
    admin = await make_user(session, "root", role="admin")
    await set_user_roles(session, admin, [roles["admin"].id])
    await session.commit()
    return roles


# -- the catalog ---------------------------------------------------------


def test_every_seeded_role_only_uses_catalogued_permissions() -> None:
    """A typo in a system role would silently grant nothing; catch it at import time."""
    for name, _description, permissions in SYSTEM_ROLES:
        assert unknown_permissions(list(permissions)) == [], f"{name} references an unknown permission"


def test_the_wildcard_expands_to_every_permission() -> None:
    assert expand(["*"]) >= set(ALL_PERMISSIONS)
    assert expand(["vms.read"]) == {"vms.read"}


def test_permissions_actually_used_by_the_product_are_all_catalogued() -> None:
    """Anything a legacy role granted must exist in the catalog, or the role editor cannot show it."""
    legacy = {item for granted in ROLE_PERMISSIONS.values() for item in granted if item != "*"}
    assert legacy <= set(PERMISSIONS), f"missing from the catalog: {sorted(legacy - set(PERMISSIONS))}"


# -- seeding and backfill ------------------------------------------------


async def test_seeding_is_idempotent_and_repairs_edited_system_roles(session) -> None:
    roles = await seeded(session)
    assert set(roles) == {name for name, _d, _p in SYSTEM_ROLES}

    roles["viewer"].permissions_json = []
    await session.commit()
    await seed_system_roles(session)
    await session.commit()

    refreshed = await session.scalar(select(Role).where(Role.name == "viewer"))
    assert refreshed.permissions_json  # the catalog owns system roles, so the edit is undone
    assert len((await session.scalars(select(Role))).all()) == len(SYSTEM_ROLES)


async def test_backfill_gives_every_existing_user_their_legacy_role(session) -> None:
    await make_user(session, "ada", role="admin")
    await make_user(session, "grace", role="operator")
    await seeded(session)

    created = await backfill_user_roles(session)
    await session.commit()

    assert created == 2
    ada = await session.scalar(select(User).where(User.username == "ada"))
    assert await effective_permissions(session, ada) >= {"*"}


async def test_backfill_never_overwrites_a_later_assignment(session) -> None:
    user = await make_user(session, "ada", role="admin")
    roles = await seeded(session)
    await set_user_roles(session, user, [roles["viewer"].id])
    await session.commit()

    assert await backfill_user_roles(session) == 0
    assert "*" not in await effective_permissions(session, user)


@pytest.mark.parametrize("role_name", ["admin", "operator", "auditor", "viewer"])
async def test_role_permissions_match_the_legacy_behaviour_exactly(session, role_name: str) -> None:
    """The migration must not silently grant or revoke anything on day one."""
    user = await make_user(session, f"user-{role_name}", role=role_name)
    roles = await seeded(session)
    await backfill_user_roles(session)
    await session.commit()

    resolved = await effective_permissions(session, user)
    legacy = ROLE_PERMISSIONS[role_name]
    if "*" in legacy:
        assert "*" in resolved
    else:
        assert resolved == set(legacy), f"{role_name} drifted: {sorted(resolved ^ set(legacy))}"
    assert roles[role_name].is_system is True


# -- roles reaching a user -----------------------------------------------


async def test_access_groups_grant_their_roles_to_members(session) -> None:
    roles = await seeded(session)
    user = await make_user(session, "ada", role="noaccess")
    group = AccessGroup(id=new_id(), name="Schedulers", role_ids_json=[roles["operator"].id])
    session.add(group)
    await session.commit()

    assert await effective_permissions(session, user) == set()
    await set_user_access_groups(session, user, [group.id])
    await session.commit()

    assert "schedules.write" in await effective_permissions(session, user)


async def test_permissions_are_the_union_of_direct_roles_and_group_roles(session) -> None:
    roles = await seeded(session)
    user = await make_user(session, "ada")
    group = AccessGroup(id=new_id(), name="Auditors", role_ids_json=[roles["auditor"].id])
    session.add(group)
    await session.commit()
    await set_user_roles(session, user, [roles["viewer"].id])
    await set_user_access_groups(session, user, [group.id])
    await session.commit()

    resolved = await effective_permissions(session, user)
    assert "audit.read" in resolved  # from the group
    assert "vms.read" in resolved  # from the direct role


async def test_the_cached_role_column_tracks_the_most_privileged_assignment(session) -> None:
    roles = await seeded(session)
    user = await make_user(session, "ada")
    await set_user_roles(session, user, [roles["viewer"].id, roles["admin"].id])
    await session.commit()

    assert user.role == "admin"


# -- lock-out guards -----------------------------------------------------


async def test_the_last_administrator_cannot_be_stripped_of_management(session) -> None:
    roles = await seeded(session)
    admin = await make_user(session, "ada", role="admin")
    await set_user_roles(session, admin, [roles["admin"].id])
    await session.commit()

    await assert_admin_remains(session)  # fine while they hold it

    await set_user_roles(session, admin, [roles["viewer"].id])
    await session.commit()
    with pytest.raises(AccessError, match="users.manage"):
        await assert_admin_remains(session)


async def test_a_disabled_administrator_does_not_count(session) -> None:
    roles = await seeded(session)
    admin = await make_user(session, "ada", role="admin", disabled=True)
    await set_user_roles(session, admin, [roles["admin"].id])
    await session.commit()

    with pytest.raises(AccessError):
        await assert_admin_remains(session)


async def test_assigning_an_unknown_role_is_refused(session) -> None:
    await seeded(session)
    user = await make_user(session, "ada")
    with pytest.raises(AccessError, match="do not exist"):
        await set_user_roles(session, user, ["not-a-role"])


# -- legacy identity provider --------------------------------------------


async def test_the_legacy_entra_row_becomes_an_identity_provider(session) -> None:
    session.add(IdentityProviderSettings(id=1, enabled=True, tenant_id="tid", client_id="cid", auto_provision=True, default_role="operator"))
    await session.commit()

    await migrate_identity_provider(session)
    await session.commit()

    provider = await session.scalar(select(IdentityProvider))
    assert provider.type == "entra" and provider.enabled is True
    assert provider.config_json["tenant_id"] == "tid"
    assert provider.config_json["default_role"] == "operator"


async def test_an_unconfigured_legacy_row_is_not_migrated(session) -> None:
    session.add(IdentityProviderSettings(id=1))
    await session.commit()
    await migrate_identity_provider(session)
    await session.commit()
    assert await session.scalar(select(IdentityProvider)) is None


# -- the API surface -----------------------------------------------------


async def test_the_permission_catalog_is_served_grouped(session) -> None:
    async with api_client(session) as client:
        response = await client.get("/api/access/permissions")

    body = response.json()
    assert response.status_code == 200
    assert {"key", "label", "group"} <= set(body[0])
    assert any(item["key"] == "users.manage" for item in body)


async def test_a_custom_role_can_be_created_edited_and_deleted(session) -> None:
    await seeded_with_admin(session)
    async with api_client(session) as client:
        created = await client.post("/api/access/roles", json={"name": "Scheduler", "description": "Runs waves", "permissions": ["schedules.read", "schedules.write"]})
        assert created.status_code == 201, created.text
        role_id = created.json()["id"]

        edited = await client.patch(f"/api/access/roles/{role_id}", json={"name": "Wave runner", "description": "", "permissions": ["schedules.read"]})
        assert edited.json()["name"] == "Wave runner"
        assert edited.json()["permissions"] == ["schedules.read"]

        assert (await client.delete(f"/api/access/roles/{role_id}")).status_code == 200


async def test_a_role_cannot_be_saved_with_a_permission_that_does_not_exist(session) -> None:
    await seeded(session)
    async with api_client(session) as client:
        response = await client.post("/api/access/roles", json={"name": "Typo", "permissions": ["schedules.writ"]})

    assert response.status_code == 422
    assert "schedules.writ" in response.text


async def test_built_in_roles_cannot_be_deleted_or_renamed(session) -> None:
    roles = await seeded_with_admin(session)
    admin_id = roles["admin"].id
    async with api_client(session) as client:
        assert (await client.delete(f"/api/access/roles/{admin_id}")).status_code == 409
        renamed = await client.patch(f"/api/access/roles/{admin_id}", json={"name": "root", "permissions": ["*"]})

    assert renamed.status_code == 409


async def test_an_access_group_reports_its_membership(session) -> None:
    roles = await seeded_with_admin(session)
    user = await make_user(session, "ada")
    async with api_client(session) as client:
        created = await client.post("/api/access/access-groups", json={"name": "Operators", "description": "", "role_ids": [roles["operator"].id]})
        group_id = created.json()["id"]
        await client.patch(f"/api/access/users/{user.id}", json={"access_group_ids": [group_id]})
        listed = (await client.get("/api/access/access-groups")).json()

    assert created.status_code == 201
    assert listed[0]["member_count"] == 1


async def test_stripping_the_last_administrator_over_the_api_is_refused(session) -> None:
    """The guard has to hold through the API too, not just the service layer."""
    roles = await seeded_with_admin(session)
    admin = await session.scalar(select(User).where(User.username == "root"))
    async with api_client(session) as client:
        response = await client.patch(f"/api/access/users/{admin.id}", json={"role_ids": [roles["viewer"].id]})

    assert response.status_code == 409
    assert "users.manage" in response.text


async def test_the_break_glass_account_is_protected(session) -> None:
    await seeded_with_admin(session)
    rescue = await make_user(session, "rescue", role="admin", is_break_glass=True)
    await backfill_user_roles(session)
    await session.commit()

    async with api_client(session) as client:
        disabled = await client.patch(f"/api/access/users/{rescue.id}", json={"disabled": True})
        removed = await client.delete(f"/api/access/users/{rescue.id}")

    assert disabled.status_code == 409
    assert removed.status_code == 409


async def test_local_login_cannot_be_turned_off_with_no_way_back_in(session) -> None:
    await seeded(session)
    async with api_client(session) as client:
        response = await client.put("/api/access/policies", json={"local_login_enabled": False})

    assert response.status_code == 409
    assert "sign-in provider" in response.text


async def test_custom_roles_and_access_groups_survive_an_export_import_round_trip(session) -> None:
    """The exporter allow-lists fields, so anything new has to be carried deliberately."""
    from app.backup import apply_import, build_export, reset_estate

    roles = await seeded(session)
    custom = Role(id=new_id(), name="Wave runner", description="Runs waves", is_system=False, permissions_json=["schedules.read", "schedules.write"])
    session.add(custom)
    await session.commit()
    session.add(AccessGroup(id=new_id(), name="On call", description="Pager rota", role_ids_json=[custom.id, roles["viewer"].id]))
    await session.commit()

    document = await build_export(session, [], [], app_version="test")
    assert [item["name"] for item in document["roles"]] == ["Wave runner"], "built-in roles must not be exported"
    assert document["access_groups"][0]["roles"] == ["Wave runner", "viewer"]

    await reset_estate(session)
    await session.execute(AccessGroup.__table__.delete())
    await session.execute(Role.__table__.delete().where(Role.is_system.is_(False)))
    await session.commit()

    summary = await apply_import(session, document, sections=["roles", "access_groups"], connections=[], connectors=[])
    await session.commit()

    assert summary["failed"] == 0, summary
    restored = await session.scalar(select(Role).where(Role.name == "Wave runner"))
    assert restored.permissions_json == ["schedules.read", "schedules.write"]
    group = await session.scalar(select(AccessGroup).where(AccessGroup.name == "On call"))
    assert restored.id in group.role_ids_json


async def test_an_imported_role_with_an_unknown_permission_is_reported_not_saved(session) -> None:
    from app.backup import apply_import

    await seeded(session)
    document = {
        "format": "azure-vm-scheduler.settings", "version": 1,
        "roles": [{"name": "Broken", "description": "", "permissions": ["schedules.writ"]}],
    }
    summary = await apply_import(session, document, sections=["roles"], connections=[], connectors=[])
    await session.commit()

    assert summary["failed"] == 1
    assert await session.scalar(select(Role).where(Role.name == "Broken")) is None


# -- identity providers over the API -------------------------------------


async def test_an_identity_provider_never_returns_its_secret(session) -> None:
    async with api_client(session) as client:
        created = await client.post("/api/access/identity-providers", json={
            "name": "Entra", "type": "entra", "enabled": True, "button_label": "Sign in",
            "config": {"tenant_id": "t", "client_id": "c"}, "client_secret": "super-secret",
        })
        listed = (await client.get("/api/access/identity-providers")).json()

    assert created.status_code == 201
    assert "super-secret" not in created.text
    assert created.json()["has_client_secret"] is True
    assert "client_secret_encrypted" not in listed[0]["config"]


async def test_saving_without_a_new_secret_keeps_the_stored_one(session) -> None:
    async with api_client(session) as client:
        created = await client.post("/api/access/identity-providers", json={
            "name": "Entra", "config": {"tenant_id": "t"}, "client_secret": "keep-me",
        })
        provider_id = created.json()["id"]
        updated = await client.patch(f"/api/access/identity-providers/{provider_id}", json={
            "name": "Entra renamed", "config": {"tenant_id": "t2"},
        })

    assert updated.json()["has_client_secret"] is True
    assert updated.json()["config"]["tenant_id"] == "t2"


async def test_testing_a_provider_reports_each_check_separately(session) -> None:
    """No issuer configured, so the checks are all local — nothing reaches the network."""
    await seeded(session)
    async with api_client(session) as client:
        response = await client.post("/api/access/identity-providers/test", json={
            "name": "Entra", "config": {"client_id": "11111111-2222-3333-4444-555555555555", "default_role": "nope"},
        })

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is False
    named = {check["name"]: check for check in body["checks"]}
    assert named["Client ID"]["ok"] is True
    assert named["Issuer"]["ok"] is False
    assert named["Client secret"]["ok"] is False
    assert named["Default role"]["ok"] is False  # unknown role, but not critical
    assert named["Default role"]["critical"] is False


async def test_testing_a_saml_provider_shows_the_urls_to_register(session) -> None:
    await seeded(session)
    async with api_client(session) as client:
        response = await client.post("/api/access/identity-providers/test", json={
            "name": "Okta", "type": "saml",
            "config": {"entity_id": "http://idp.example/metadata", "sso_url": "https://idp.example/sso"},
        })

    body = response.json()
    named = {check["name"]: check for check in body["checks"]}
    assert named["IdP Entity ID"]["ok"] is True
    assert named["IdP SSO URL"]["ok"] is True
    assert named["Signing certificate"]["ok"] is False, "no certificate was supplied"
    # The administrator has to paste these into the identity provider, so they must be shown.
    assert "/api/auth/saml/" in named["Reply URL (ACS)"]["detail"]
    assert named["Identifier (Entity ID)"]["detail"].endswith("/api/auth/saml/metadata")
