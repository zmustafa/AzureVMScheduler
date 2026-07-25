"""Just-in-time user provisioning and group-to-role mapping, shared by OIDC and SAML.

The security-critical part is how an assertion is matched to an existing account. See
``provision_sso_user`` — the rules there are what stop a crafted assertion from taking over the
local administrator.
"""

from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .access import set_user_roles
from .models import IdentityProvider, Role, User, utcnow
from .permissions import NO_ACCESS_ROLE


class ProvisioningError(Exception):
    """Raised when an assertion is valid but the user must not be signed in."""


async def _role_ids_for_names(db: AsyncSession, names: list[str]) -> list[str]:
    if not names:
        return []
    rows = (await db.execute(select(Role.id, Role.name).where(Role.name.in_(names)))).all()
    by_name = {name: role_id for role_id, name in rows}
    return [by_name[name] for name in names if name in by_name]


def _mapped_role_names(config: dict[str, Any], groups: list[str]) -> list[str]:
    """Roles implied by the assertion's groups, matched case-insensitively."""
    mapping = config.get("group_role_map") or {}
    if not isinstance(mapping, dict) or not groups:
        return []
    lowered = {str(key).strip().casefold(): str(value) for key, value in mapping.items()}
    names: list[str] = []
    for group in groups:
        role = lowered.get(str(group).strip().casefold())
        if role and role not in names:
            names.append(role)
    return names


async def provision_sso_user(
    db: AsyncSession,
    provider: IdentityProvider,
    *,
    external_id: str,
    email: str,
    display_name: str = "",
    groups: list[str] | None = None,
    email_verified: bool = True,
) -> User:
    """Find or create the user behind an SSO assertion and apply group-to-role mapping.

    Account-takeover defence. The primary match is the cryptographic ``(provider, external_id)``
    subject, which the identity provider signed. Matching on email alone is dangerous: an
    assertion claiming ``email=admin`` must never seize the local administrator. So email linking
    is allowed **only** onto an account that is already SSO-managed — never one with a password —
    and only when the provider asserts the address is verified.
    """
    config = provider.config_json or {}
    groups = groups or []
    email = (email or "").strip().lower()
    if not external_id:
        raise ProvisioningError("The assertion carried no stable subject")

    user = await db.scalar(
        select(User).where(User.external_tenant_id == provider.id, User.external_oid == external_id)
    )

    if user is None and email and email_verified:
        candidate = await db.scalar(select(User).where(func.lower(User.email) == email))
        if candidate is not None:
            if candidate.password_hash:
                raise ProvisioningError(
                    "An account with this email already signs in with a password. "
                    "Link it from the users page before using single sign-on."
                )
            if candidate.external_oid and candidate.external_oid != external_id:
                raise ProvisioningError("This account is already linked to a different identity")
            user = candidate

    if user is None:
        if not config.get("auto_provision"):
            raise ProvisioningError("This account has not been provisioned")
        username = (email or f"{provider.type}-{external_id}")[:100]
        if await db.scalar(select(User.id).where(func.lower(User.username) == username.lower())):
            username = f"{username[:80]}-{secrets.token_hex(4)}"
        user = User(
            username=username,
            email=email or None,
            # No usable password: this account can only ever arrive through the provider.
            password_hash=None,
            auth_source=provider.type,
            role=NO_ACCESS_ROLE,
            must_change_password=False,
        )
        db.add(user)
        await db.flush()
        # A newly provisioned user starts with the default role, which is `noaccess` unless an
        # administrator deliberately chose otherwise — auto-provisioning never grants access.
        default_role = str(config.get("default_role") or NO_ACCESS_ROLE)
        await set_user_roles(db, user, await _role_ids_for_names(db, [default_role]))

    if user.disabled:
        raise ProvisioningError("Account is disabled")

    user.external_tenant_id = provider.id
    user.external_oid = external_id
    if email:
        user.email = email
    user.auth_source = "linked" if user.password_hash else provider.type
    user.last_login_at = utcnow()

    # Group mapping is authoritative when configured: it re-applies on every sign-in so removing
    # someone from a directory group takes their role away here too.
    mapped = _mapped_role_names(config, groups)
    if mapped:
        await set_user_roles(db, user, await _role_ids_for_names(db, mapped))

    await db.flush()
    return user
