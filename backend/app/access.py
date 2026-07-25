"""Access control: roles, access groups, effective permissions, and identity providers.

Effective permissions are the union of a user's directly-assigned roles and the roles carried by
every access group they belong to. They are resolved once per request in ``current_session`` and
cached on the user instance, so ``has_permission`` stays synchronous for its many call sites.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AccessGroup, IdentityProvider, IdentityProviderSettings, Role, User, UserAccessGroup, UserRole, new_id
from .permissions import SYSTEM_ROLES, expand


#: Where resolved permissions are cached on a User instance for the life of one request.
PERMISSION_ATTR = "_effective_permissions"


class AccessError(ValueError):
    """A change that would leave the system unusable or is otherwise not allowed."""


# -- seeding -------------------------------------------------------------


async def seed_system_roles(db: AsyncSession) -> None:
    """Create or refresh the built-in roles.

    Their permission sets are owned by the catalog, not the database: adding a capability to the
    product must not leave admin unable to use it. Custom roles are never touched.
    """
    existing = {role.name: role for role in (await db.scalars(select(Role))).all()}
    for name, description, permissions in SYSTEM_ROLES:
        role = existing.get(name)
        if role is None:
            db.add(Role(id=new_id(), name=name, description=description, is_system=True, permissions_json=list(permissions)))
        else:
            role.is_system = True
            role.description = description
            role.permissions_json = list(permissions)
    await db.flush()


async def backfill_user_roles(db: AsyncSession) -> int:
    """Give every user a role row matching the legacy ``users.role`` string.

    Runs once at startup and is idempotent: a user who already has any role assignment is left
    alone, so an administrator's later changes are never undone by a restart.
    """
    roles = {role.name: role.id for role in (await db.scalars(select(Role))).all()}
    assigned = {row for row in (await db.scalars(select(UserRole.user_id))).all()}
    created = 0
    for user in (await db.scalars(select(User))).all():
        if user.id in assigned:
            continue
        role_id = roles.get(user.role) or roles.get("viewer")
        if role_id:
            db.add(UserRole(user_id=user.id, role_id=role_id))
            created += 1
    await db.flush()
    return created


async def migrate_identity_provider(db: AsyncSession) -> None:
    """Fold the legacy single Entra row into the identity_providers table, once."""
    if await db.scalar(select(IdentityProvider.id).limit(1)):
        return
    legacy = await db.get(IdentityProviderSettings, 1)
    if not legacy or not (legacy.tenant_id or legacy.client_id or legacy.client_secret_encrypted):
        return
    db.add(IdentityProvider(
        id=new_id(),
        name="Microsoft Entra ID",
        type="entra",
        enabled=bool(legacy.enabled),
        button_label="Sign in with Microsoft",
        config_json={
            "tenant_id": legacy.tenant_id or "",
            "client_id": legacy.client_id or "",
            "client_secret_encrypted": legacy.client_secret_encrypted,
            "auto_provision": bool(legacy.auto_provision),
            "default_role": legacy.default_role or "viewer",
            "group_role_map": {},
        },
    ))
    await db.flush()


# -- effective permissions -----------------------------------------------


async def role_ids_for(db: AsyncSession, user_id: str) -> set[str]:
    """Every role reaching a user: assigned directly, or via one of their access groups."""
    direct = set((await db.scalars(select(UserRole.role_id).where(UserRole.user_id == user_id))).all())
    group_ids = (await db.scalars(select(UserAccessGroup.access_group_id).where(UserAccessGroup.user_id == user_id))).all()
    if group_ids:
        for group in (await db.scalars(select(AccessGroup).where(AccessGroup.id.in_(list(group_ids))))).all():
            direct.update(str(item) for item in (group.role_ids_json or []))
    return direct


async def effective_permissions(db: AsyncSession, user: User) -> set[str]:
    """Resolve what a user may actually do, right now."""
    ids = await role_ids_for(db, user.id)
    if not ids:
        return set()
    granted: set[str] = set()
    for role in (await db.scalars(select(Role).where(Role.id.in_(list(ids))))).all():
        granted |= expand(role.permissions_json or [])
    return granted


def cache_permissions(user: User, permissions: set[str]) -> None:
    object.__setattr__(user, PERMISSION_ATTR, permissions)


def cached_permissions(user: User) -> set[str] | None:
    return getattr(user, PERMISSION_ATTR, None)


# -- guards --------------------------------------------------------------


async def assert_admin_remains(db: AsyncSession, *, ignore_user_id: str | None = None) -> None:
    """Refuse a change that would leave nobody able to administer the system.

    Roles are editable, so without this an administrator can lock everyone out with two clicks and
    the only way back in is editing the database by hand.
    """
    admin_roles = [
        role.id for role in (await db.scalars(select(Role))).all()
        if "*" in (role.permissions_json or []) or "users.manage" in (role.permissions_json or [])
    ]
    if not admin_roles:
        raise AccessError("At least one role must keep the users.manage permission")
    if not await db.scalar(select(User.id).limit(1)):
        # No accounts exist yet, so there is nobody to lock out. An empty table is a fresh install;
        # accounts that all happen to be *disabled* is a genuine lock-out and still fails below.
        return
    for user in (await db.scalars(select(User).where(User.disabled.is_(False)))).all():
        if ignore_user_id and user.id == ignore_user_id:
            continue
        if await role_ids_for(db, user.id) & set(admin_roles):
            return
    raise AccessError("At least one enabled user must keep a role that grants users.manage")


async def set_user_roles(db: AsyncSession, user: User, role_ids: list[str]) -> None:
    """Replace a user's direct role assignments, keeping users.role as a readable cache."""
    found = (await db.scalars(select(Role).where(Role.id.in_(role_ids or [])))).all()
    if len(found) != len(set(role_ids or [])):
        raise AccessError("One or more roles do not exist")
    await db.execute(delete(UserRole).where(UserRole.user_id == user.id))
    for role_id in dict.fromkeys(role_ids or []):
        db.add(UserRole(user_id=user.id, role_id=role_id))
    user.role = _primary_role_name(found)
    await db.flush()


async def set_user_access_groups(db: AsyncSession, user: User, group_ids: list[str]) -> None:
    found = (await db.scalars(select(AccessGroup.id).where(AccessGroup.id.in_(group_ids or [])))).all()
    if len(found) != len(set(group_ids or [])):
        raise AccessError("One or more access groups do not exist")
    await db.execute(delete(UserAccessGroup).where(UserAccessGroup.user_id == user.id))
    for group_id in dict.fromkeys(group_ids or []):
        db.add(UserAccessGroup(user_id=user.id, access_group_id=group_id))
    await db.flush()


def _primary_role_name(roles: list[Role]) -> str:
    """The most privileged assigned role, cached on users.role for display and legacy callers."""
    if not roles:
        return "noaccess"
    ranked = sorted(roles, key=lambda role: (-len(expand(role.permissions_json or [])), role.name))
    return ranked[0].name


# -- serialisation -------------------------------------------------------


def role_view(role: Role) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "is_system": role.is_system,
        "permissions": list(role.permissions_json or []),
    }


def access_group_view(group: AccessGroup, member_count: int = 0) -> dict[str, Any]:
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "role_ids": [str(item) for item in (group.role_ids_json or [])],
        "member_count": member_count,
    }


#: Config keys that hold a secret and must never leave the server.
IDP_SECRET_KEYS = ("client_secret_encrypted",)


def identity_provider_view(provider: IdentityProvider) -> dict[str, Any]:
    config = dict(provider.config_json or {})
    has_secret = bool(config.get("client_secret_encrypted"))
    for key in IDP_SECRET_KEYS:
        config.pop(key, None)
    return {
        "id": provider.id,
        "name": provider.name,
        "type": provider.type,
        "enabled": provider.enabled,
        "button_label": provider.button_label,
        "config": config,
        "has_client_secret": has_secret,
    }
