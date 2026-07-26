"""The /api/access surface: users, roles, access groups, sessions, policies and sign-in providers.

Kept in its own router because main.py is already large and this is a self-contained feature. Every
route requires ``users.manage``; administrators hold it through the ``*`` wildcard, so nothing
changes for them, but the capability can now be delegated to a custom role.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .access import (
    AccessError,
    access_group_view,
    assert_admin_remains,
    identity_provider_view,
    role_view,
    seed_system_roles,
    set_user_access_groups,
    set_user_roles,
)
from .audit import audit
from .auth import get_security_policy, hash_password, require_csrf, require_permission, validate_password
from .connections import encrypt_value
from .database import get_db
from . import oidc, saml
from .models import AccessGroup, Group, IdentityProvider, ImportBatch, LoginSession, Role, Schedule, User, UserAccessGroup, UserRole, VirtualMachine, new_id, utcnow
from .permissions import PERMISSION_GROUPS, SYSTEM_ROLE_NAMES, unknown_permissions
from .schemas import (
    AccessGroupInput,
    IdentityProviderInput,
    PasswordReset,
    RoleInput,
    SecurityPolicyUpdate,
    UserCreate,
    UserUpdate,
)

router = APIRouter(prefix="/api/access", tags=["access"])

#: One dependency for the whole router so the capability is impossible to forget on a new route.
Manage = Depends(require_permission("users.manage"))


def _fail(exc: AccessError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


# -- catalog -------------------------------------------------------------


@router.get("/permissions")
async def list_permissions(user: User = Manage) -> list[dict[str, str]]:
    """The capability catalog, grouped, so the role editor renders readable sections."""
    return [
        {"key": key, "label": label, "group": section}
        for section, items in PERMISSION_GROUPS
        for key, label in items
    ]


# -- users ---------------------------------------------------------------


async def _user_payload(db: AsyncSession, user: User) -> dict[str, Any]:
    role_ids = list((await db.scalars(select(UserRole.role_id).where(UserRole.user_id == user.id))).all())
    group_ids = list((await db.scalars(select(UserAccessGroup.access_group_id).where(UserAccessGroup.user_id == user.id))).all())
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "role_ids": role_ids,
        "access_group_ids": group_ids,
        "auth_source": user.auth_source,
        "disabled": user.disabled,
        "is_break_glass": user.is_break_glass,
        "must_change_password": user.must_change_password,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
        "locked_until": user.locked_until,
    }


@router.get("/users")
async def list_users(user: User = Manage, db: AsyncSession = Depends(get_db)):
    users = (await db.scalars(select(User).order_by(func.lower(User.username)))).all()
    return [await _user_payload(db, item) for item in users]


@router.post("/users", status_code=201)
async def create_user(payload: UserCreate, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    if await db.scalar(select(User.id).where(func.lower(User.username) == payload.username.lower())):
        raise HTTPException(status_code=409, detail="That username is already taken")
    policy = await get_security_policy(db)
    problems = validate_password(payload.password, policy)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))
    created = User(
        id=new_id(),
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        must_change_password=True,
    )
    db.add(created)
    await db.flush()
    try:
        await set_user_roles(db, created, await _role_ids_for_names(db, payload.role, payload.role_ids))
        await set_user_access_groups(db, created, payload.access_group_ids or [])
    except AccessError as exc:
        raise _fail(exc) from exc
    audit(db, actor, "user.created", "user", created.id, {"username": created.username, "role": created.role})
    await db.commit()
    return await _user_payload(db, created)


@router.patch("/users/{user_id}")
async def update_user(user_id: str, payload: UserUpdate, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    values = payload.model_dump(exclude_unset=True)

    if "disabled" in values and values["disabled"] and target.is_break_glass:
        raise HTTPException(status_code=409, detail="The break-glass administrator cannot be disabled")
    if "email" in values:
        target.email = values["email"]
    if "disabled" in values:
        target.disabled = bool(values["disabled"])
    try:
        if "role_ids" in values or "role" in values:
            await set_user_roles(db, target, await _role_ids_for_names(db, values.get("role"), values.get("role_ids")))
        if "access_group_ids" in values:
            await set_user_access_groups(db, target, values["access_group_ids"] or [])
        # Checked after the change so the guard sees the world the caller is asking for.
        await assert_admin_remains(db)
    except AccessError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    audit(db, actor, "user.updated", "user", target.id, {key: values[key] for key in values if key != "password"})
    await db.commit()
    return await _user_payload(db, target)


@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: str, payload: PasswordReset, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    policy = await get_security_policy(db)
    problems = validate_password(payload.new_password, policy)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))
    target.password_hash = hash_password(payload.new_password)
    target.must_change_password = True
    target.failed_login_count = 0
    target.locked_until = None
    audit(db, actor, "user.password_reset", "user", target.id, {"username": target.username})
    await db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_user_sessions(user_id: str, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    sessions = (await db.scalars(select(LoginSession).where(LoginSession.user_id == user_id, LoginSession.revoked_at.is_(None)))).all()
    for session in sessions:
        session.revoked_at = utcnow()
    audit(db, actor, "user.sessions_revoked", "user", user_id, {"count": len(sessions)})
    await db.commit()
    return {"revoked": len(sessions)}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.is_break_glass:
        raise HTTPException(status_code=409, detail="The break-glass administrator cannot be deleted")
    if target.id == actor.id:
        raise HTTPException(status_code=409, detail="You cannot delete your own account")
    try:
        await assert_admin_remains(db, ignore_user_id=target.id)
    except AccessError as exc:
        raise _fail(exc) from exc
    # Applications, machines, schedules and import batches carry a nullable created_by pointing at
    # this row. Deleting the user without releasing them trips the foreign key and the request dies
    # with a 500, which in practice means an account that has ever created anything can never be
    # off-boarded. The estate is shared, not owned, so the attribution is what is dropped.
    for model in (Group, VirtualMachine, Schedule, ImportBatch):
        await db.execute(update(model).where(model.created_by == user_id).values(created_by=None))
    await db.delete(target)
    audit(db, actor, "user.deleted", "user", user_id, {"username": target.username})
    await db.commit()
    return {"ok": True}


# -- roles ---------------------------------------------------------------


@router.get("/roles")
async def list_roles(user: User = Manage, db: AsyncSession = Depends(get_db)):
    roles = (await db.scalars(select(Role).order_by(Role.is_system.desc(), func.lower(Role.name)))).all()
    return [role_view(role) for role in roles]


@router.post("/roles", status_code=201)
async def create_role(payload: RoleInput, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    _assert_permissions_known(payload.permissions)
    if await db.scalar(select(Role.id).where(func.lower(Role.name) == payload.name.lower())):
        raise HTTPException(status_code=409, detail="A role with that name already exists")
    role = Role(id=new_id(), name=payload.name, description=payload.description, is_system=False, permissions_json=payload.permissions)
    db.add(role)
    audit(db, actor, "role.created", "role", role.id, {"name": role.name, "permissions": len(payload.permissions)})
    await db.commit()
    return role_view(role)


@router.patch("/roles/{role_id}")
async def update_role(role_id: str, payload: RoleInput, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    role = await db.get(Role, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    _assert_permissions_known(payload.permissions)
    if role.is_system and payload.name != role.name:
        raise HTTPException(status_code=409, detail="A built-in role cannot be renamed")
    role.description = payload.description
    role.permissions_json = payload.permissions
    if not role.is_system:
        role.name = payload.name
    try:
        await assert_admin_remains(db)
    except AccessError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    audit(db, actor, "role.updated", "role", role.id, {"name": role.name, "permissions": len(payload.permissions)})
    await db.commit()
    return role_view(role)


@router.delete("/roles/{role_id}")
async def delete_role(role_id: str, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    role = await db.get(Role, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.is_system or role.name in SYSTEM_ROLE_NAMES:
        raise HTTPException(status_code=409, detail="Built-in roles cannot be deleted")
    await db.execute(delete(UserRole).where(UserRole.role_id == role_id))
    for group in (await db.scalars(select(AccessGroup))).all():
        if role_id in (group.role_ids_json or []):
            group.role_ids_json = [item for item in group.role_ids_json if item != role_id]
    await db.delete(role)
    try:
        await assert_admin_remains(db)
    except AccessError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    audit(db, actor, "role.deleted", "role", role_id, {"name": role.name})
    await db.commit()
    return {"ok": True}


@router.post("/roles/reseed")
async def reseed_roles(actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    """Restore the built-in roles to their catalog definitions. Custom roles are untouched."""
    await _assert_manage(actor)
    await seed_system_roles(db)
    audit(db, actor, "role.reseeded", "role", None, {})
    await db.commit()
    return {"ok": True}


# -- access groups -------------------------------------------------------


@router.get("/access-groups")
async def list_access_groups(user: User = Manage, db: AsyncSession = Depends(get_db)):
    groups = (await db.scalars(select(AccessGroup).order_by(func.lower(AccessGroup.name)))).all()
    counts = dict((await db.execute(
        select(UserAccessGroup.access_group_id, func.count()).group_by(UserAccessGroup.access_group_id)
    )).all())
    return [access_group_view(group, int(counts.get(group.id, 0))) for group in groups]


@router.post("/access-groups", status_code=201)
async def create_access_group(payload: AccessGroupInput, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    if await db.scalar(select(AccessGroup.id).where(func.lower(AccessGroup.name) == payload.name.lower())):
        raise HTTPException(status_code=409, detail="An access group with that name already exists")
    await _assert_roles_exist(db, payload.role_ids)
    group = AccessGroup(id=new_id(), name=payload.name, description=payload.description, role_ids_json=payload.role_ids)
    db.add(group)
    audit(db, actor, "access_group.created", "access_group", group.id, {"name": group.name})
    await db.commit()
    return access_group_view(group)


@router.patch("/access-groups/{group_id}")
async def update_access_group(group_id: str, payload: AccessGroupInput, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    group = await db.get(AccessGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Access group not found")
    await _assert_roles_exist(db, payload.role_ids)
    group.name = payload.name
    group.description = payload.description
    group.role_ids_json = payload.role_ids
    try:
        await assert_admin_remains(db)
    except AccessError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    audit(db, actor, "access_group.updated", "access_group", group.id, {"name": group.name})
    await db.commit()
    return access_group_view(group)


@router.delete("/access-groups/{group_id}")
async def delete_access_group(group_id: str, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    group = await db.get(AccessGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Access group not found")
    await db.execute(delete(UserAccessGroup).where(UserAccessGroup.access_group_id == group_id))
    await db.delete(group)
    try:
        await assert_admin_remains(db)
    except AccessError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    audit(db, actor, "access_group.deleted", "access_group", group_id, {"name": group.name})
    await db.commit()
    return {"ok": True}


# -- sessions ------------------------------------------------------------


@router.get("/sessions")
async def list_sessions(user: User = Manage, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(LoginSession, User.username)
        .join(User, User.id == LoginSession.user_id)
        .order_by(LoginSession.last_seen_at.desc())
        .limit(500)
    )).all()
    return [{
        "id": session.id,
        "user_id": session.user_id,
        "username": username,
        "auth_method": session.auth_method,
        "created_at": session.created_at,
        "last_seen_at": session.last_seen_at,
        "expires_at": session.expires_at,
        "revoked_at": session.revoked_at,
        "ip_address": session.ip_address,
        "user_agent": session.user_agent,
    } for session, username in rows]


@router.post("/sessions/{session_id}/revoke")
async def revoke_session(session_id: str, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    session = await db.get(LoginSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.revoked_at = utcnow()
    audit(db, actor, "session.revoked", "login_session", session_id, {})
    await db.commit()
    return {"ok": True}


@router.post("/sessions/revoke-expired")
async def revoke_expired_sessions(actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    now = utcnow()
    stale = (await db.scalars(select(LoginSession).where(LoginSession.revoked_at.is_(None), LoginSession.expires_at <= now))).all()
    for session in stale:
        session.revoked_at = now
    audit(db, actor, "session.revoked_expired", "login_session", None, {"count": len(stale)})
    await db.commit()
    return {"revoked": len(stale)}


# -- policies ------------------------------------------------------------


@router.get("/policies")
async def read_policies(user: User = Manage, db: AsyncSession = Depends(get_db)):
    policy = await get_security_policy(db)
    return {
        "local_login_enabled": policy.local_login_enabled,
        "password_min_length": policy.password_min_length,
        "password_require_upper": policy.password_require_upper,
        "password_require_lower": policy.password_require_lower,
        "password_require_number": policy.password_require_number,
        "password_require_symbol": policy.password_require_symbol,
        "lockout_attempts": policy.lockout_attempts,
        "lockout_minutes": policy.lockout_minutes,
        "ip_lockout_enabled": policy.ip_lockout_enabled,
        "ip_lockout_attempts": policy.ip_lockout_attempts,
        "ip_lockout_window_seconds": policy.ip_lockout_window_seconds,
        "ip_lockout_seconds": policy.ip_lockout_seconds,
        "allow_self_registration": policy.allow_self_registration,
        "session_idle_minutes": policy.session_idle_minutes,
        "session_absolute_hours": policy.session_absolute_hours,
    }


@router.put("/policies")
async def update_policies(payload: SecurityPolicyUpdate, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    policy = await get_security_policy(db)
    values = payload.model_dump(exclude_unset=True)
    if values.get("local_login_enabled") is False and not await _any_enabled_provider(db):
        raise HTTPException(status_code=409, detail="Enable a sign-in provider before turning off username and password login")
    for key, value in values.items():
        setattr(policy, key, value)
    audit(db, actor, "security_policy.updated", "security_policy", "1", values)
    await db.commit()
    return await read_policies(actor, db)


# -- identity providers --------------------------------------------------


@router.get("/identity-providers")
async def list_identity_providers(user: User = Manage, db: AsyncSession = Depends(get_db)):
    providers = (await db.scalars(select(IdentityProvider).order_by(func.lower(IdentityProvider.name)))).all()
    return [identity_provider_view(provider) for provider in providers]


@router.post("/identity-providers", status_code=201)
async def create_identity_provider(payload: IdentityProviderInput, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    provider = IdentityProvider(id=new_id(), name=payload.name, type=payload.type, enabled=payload.enabled, button_label=payload.button_label)
    provider.config_json = _merge_config({}, payload)
    db.add(provider)
    audit(db, actor, "identity_provider.created", "identity_provider", provider.id, {"name": provider.name, "type": provider.type})
    await db.commit()
    return identity_provider_view(provider)


@router.patch("/identity-providers/{provider_id}")
async def update_identity_provider(provider_id: str, payload: IdentityProviderInput, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    provider = await db.get(IdentityProvider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Identity provider not found")
    provider.name = payload.name
    provider.type = payload.type
    provider.enabled = payload.enabled
    provider.button_label = payload.button_label
    provider.config_json = _merge_config(provider.config_json or {}, payload)
    audit(db, actor, "identity_provider.updated", "identity_provider", provider.id, {"name": provider.name, "enabled": provider.enabled})
    await db.commit()
    return identity_provider_view(provider)


@router.delete("/identity-providers/{provider_id}")
async def delete_identity_provider(provider_id: str, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    await _assert_manage(actor)
    provider = await db.get(IdentityProvider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Identity provider not found")
    policy = await get_security_policy(db)
    if not policy.local_login_enabled and not await _any_enabled_provider(db, ignore_id=provider_id):
        raise HTTPException(status_code=409, detail="This is the only way in — enable local login before removing it")
    await db.delete(provider)
    audit(db, actor, "identity_provider.deleted", "identity_provider", provider_id, {"name": provider.name})
    await db.commit()
    return {"ok": True}


@router.post("/identity-providers/test")
async def test_identity_provider(payload: IdentityProviderInput, request: Request, actor: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    """Check a provider's configuration and report each finding separately.

    Deliberately returns 200 with a list of checks rather than an error: a half-configured provider
    should show you exactly which parts are wrong, not just the first thing that failed.
    """
    await _assert_manage(actor)
    config = dict(payload.config or {})
    existing: dict[str, Any] = {}
    if payload.id:
        found = await db.get(IdentityProvider, payload.id)
        existing = dict(found.config_json or {}) if found else {}
    # The editor never echoes the stored secret back, so merge it in before testing.
    if payload.client_secret:
        config["client_secret_encrypted"] = encrypt_value(payload.client_secret)
    elif existing.get("client_secret_encrypted"):
        config["client_secret_encrypted"] = existing["client_secret_encrypted"]

    roles = {role.name for role in (await db.scalars(select(Role))).all()}
    checks: list[dict[str, Any]] = [
        {"name": "Display name", "ok": bool(payload.name.strip()), "critical": True,
         "detail": "Shown on the sign-in button" if payload.name.strip() else "Give the provider a name"},
    ]

    if payload.type == "saml":
        base = saml.public_base_url(request)
        checks.extend(saml.test_config(config, base, payload.id or "new"))
    else:
        checks.append({
            "name": "Client ID", "ok": bool(str(config.get("client_id") or "").strip()), "critical": True,
            "detail": str(config.get("client_id") or "") or "Copy the application (client) ID from the app registration",
        })
        checks.append({
            "name": "Client secret", "ok": bool(config.get("client_secret_encrypted")), "critical": True,
            "detail": "Stored encrypted" if config.get("client_secret_encrypted") else "Add a client secret",
        })
        # Reaches the issuer for real, so a wrong tenant or unreachable host is caught here rather
        # than at somebody's first sign-in attempt.
        checks.extend(await oidc.test_config(config))
        checks.append({
            "name": "Redirect URI", "ok": True, "critical": False,
            "detail": f"{saml.public_base_url(request)}/api/auth/oidc/{payload.id or '<id>'}/callback",
        })

    default_role = str(config.get("default_role") or "").strip()
    checks.append({
        "name": "Default role",
        "ok": (not default_role) or default_role in roles,
        "critical": False,
        "detail": f"Auto-provisioned users become {default_role}" if default_role in roles else f"Unknown role: {default_role or 'not set'}",
    })
    mapped = {str(value) for value in (config.get("group_role_map") or {}).values()}
    unknown = sorted(mapped - roles)
    checks.append({
        "name": "Group to role mapping",
        "ok": not unknown,
        "critical": False,
        "detail": "Every mapped role exists" if not unknown else f"Unknown role(s): {', '.join(unknown)}",
    })

    failed_critical = [item for item in checks if not item["ok"] and item["critical"]]
    ok = not failed_critical
    return {
        "ok": ok,
        "summary": "Configuration looks complete" if ok else f"{len(failed_critical)} required setting(s) still missing",
        "checks": checks,
    }


# -- helpers -------------------------------------------------------------


def _looks_like_guid(value: str) -> bool:
    parts = value.split("-")
    return len(parts) == 5 and all(part and all(char in "0123456789abcdefABCDEF" for char in part) for part in parts)


async def _any_enabled_provider(db: AsyncSession, ignore_id: str | None = None) -> bool:
    statement = select(IdentityProvider.id).where(IdentityProvider.enabled.is_(True))
    if ignore_id:
        statement = statement.where(IdentityProvider.id != ignore_id)
    return bool(await db.scalar(statement.limit(1)))


def _merge_config(current: dict[str, Any], payload: IdentityProviderInput) -> dict[str, Any]:
    """Keep the stored secret unless a new one was typed, so saving the form cannot wipe it."""
    merged = {**current, **(payload.config or {})}
    merged.pop("client_secret", None)
    if payload.client_secret:
        merged["client_secret_encrypted"] = encrypt_value(payload.client_secret)
    elif current.get("client_secret_encrypted"):
        merged["client_secret_encrypted"] = current["client_secret_encrypted"]
    return merged


async def _assert_manage(user: User) -> None:
    from .auth import has_permission

    if not has_permission(user, "users.manage"):
        raise HTTPException(status_code=403, detail="Permission required: users.manage")


def _assert_permissions_known(permissions: list[str]) -> None:
    unknown = unknown_permissions(permissions)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown permission(s): {', '.join(unknown)}")


async def _assert_roles_exist(db: AsyncSession, role_ids: list[str]) -> None:
    found = (await db.scalars(select(Role.id).where(Role.id.in_(role_ids or [])))).all()
    if len(set(found)) != len(set(role_ids or [])):
        raise HTTPException(status_code=422, detail="One or more roles do not exist")


async def _role_ids_for_names(db: AsyncSession, role_name: str | None, role_ids: list[str] | None) -> list[str]:
    """Accept either explicit role ids or the legacy single role name."""
    if role_ids is not None:
        return role_ids
    if role_name:
        found = await db.scalar(select(Role.id).where(Role.name == role_name))
        if not found:
            raise HTTPException(status_code=422, detail=f"Unknown role: {role_name}")
        return [found]
    return []
