from __future__ import annotations

import hashlib
import re
import secrets
from contextlib import suppress
from datetime import timedelta
from collections.abc import Callable
from urllib.parse import urlparse

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .database import SessionLocal, get_db
from .models import IdentityProviderSettings, LoginSession, SecurityPolicy, User, utcnow


SESSION_COOKIE = "azureops_session"
CSRF_COOKIE = "azureops_csrf"
_password_hasher = PasswordHasher()

#: Verified against when the account does not exist or has no local password, so an unknown
#: username costs the same Argon2 work as a known one. Without it the sign-in latency is a
#: reliable username oracle even though the response body is identical.
_DUMMY_HASH = _password_hasher.hash(secrets.token_urlsafe(32))

#: Fallback used only when permissions could not be resolved from the database — a user object
#: built outside a request, or a role row that has gone missing. Real authorisation comes from the
#: roles table via app.access.effective_permissions.
ROLE_PERMISSIONS = {
    "admin": {"*"},
    "operator": {"schedules.read", "schedules.write", "groups.read", "groups.write", "vms.read", "vms.write", "runs.read", "imports.write", "dashboard.read", "connectors.read", "notifications.read", "notifications.manage"},
    "auditor": {"schedules.read", "groups.read", "vms.read", "runs.read", "dashboard.read", "audit.read", "connectors.read", "notifications.read"},
    "viewer": {"schedules.read", "groups.read", "vms.read", "runs.read", "dashboard.read", "connectors.read", "notifications.read"},
    "noaccess": set(),
}


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def verify_dummy_password(password: str) -> None:
    """Burn one Argon2 verification so a miss and a hit take the same time."""
    with suppress(Exception):
        _password_hasher.verify(_DUMMY_HASH, password)


def needs_rehash(password_hash: str | None) -> bool:
    """True when the stored hash used weaker parameters than the current Argon2 settings.

    Lets a successful sign-in transparently upgrade an old hash, so raising the cost factor
    protects existing accounts instead of only new ones.
    """
    if not password_hash:
        return False
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return False


def validate_password(password: str, settings: Settings | SecurityPolicy | None = None) -> list[str]:
    settings = settings or get_settings()
    errors: list[str] = []
    if len(password) < settings.password_min_length:
        errors.append(f"Use at least {settings.password_min_length} characters")
    checks = [
        (settings.password_require_upper, r"[A-Z]", "Add an uppercase letter"),
        (settings.password_require_lower, r"[a-z]", "Add a lowercase letter"),
        (settings.password_require_number, r"\d", "Add a number"),
        (settings.password_require_symbol, r"[^A-Za-z0-9]", "Add a symbol"),
    ]
    errors.extend(message for required, pattern, message in checks if required and not re.search(pattern, password))
    return errors


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def bootstrap_admin() -> None:
    settings = get_settings()
    async with SessionLocal() as db:
        policy = await db.get(SecurityPolicy, 1)
        if not policy:
            db.add(SecurityPolicy(id=1, session_absolute_hours=settings.session_hours, password_min_length=settings.password_min_length, password_require_upper=settings.password_require_upper, password_require_lower=settings.password_require_lower, password_require_number=settings.password_require_number, password_require_symbol=settings.password_require_symbol))
        if not await db.get(IdentityProviderSettings, 1):
            db.add(IdentityProviderSettings(id=1))
        existing = await db.scalar(select(User).where(User.username == settings.bootstrap_admin_username))
        if not existing:
            db.add(User(username=settings.bootstrap_admin_username, password_hash=hash_password(settings.bootstrap_admin_password), role="admin", must_change_password=True, is_break_glass=True))
        elif not existing.is_break_glass:
            existing.is_break_glass = True
        await db.commit()


async def bootstrap_access() -> None:
    """Seed the built-in roles, then give every existing user a matching assignment.

    Ordered after bootstrap_admin so the break-glass account exists to receive its admin role.
    """
    from .access import backfill_user_roles, migrate_identity_provider, seed_system_roles

    async with SessionLocal() as db:
        await seed_system_roles(db)
        await backfill_user_roles(db)
        await migrate_identity_provider(db)
        await db.commit()


async def get_security_policy(db: AsyncSession) -> SecurityPolicy:
    policy = await db.get(SecurityPolicy, 1)
    if not policy:
        settings = get_settings()
        policy = SecurityPolicy(id=1, session_absolute_hours=settings.session_hours)
        db.add(policy)
        await db.flush()
    return policy


async def get_identity_provider(db: AsyncSession) -> IdentityProviderSettings:
    provider = await db.get(IdentityProviderSettings, 1)
    if not provider:
        provider = IdentityProviderSettings(id=1)
        db.add(provider)
        await db.flush()
    return provider


def cookies_are_secure(request: Request | None) -> bool:
    """Whether the session cookies may carry the Secure attribute.

    Keyed off the transport actually in use rather than only off ``ENVIRONMENT``: a deployment
    that serves the app over TLS but forgets to set ``ENVIRONMENT=production`` would otherwise
    hand the browser a cookie it is willing to replay over plain HTTP.
    """
    settings = get_settings()
    if settings.environment.lower() == "production":
        return True
    if request is None:
        return False
    if request.url.scheme == "https":
        return True
    if settings.trust_forwarded_headers:
        return (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() == "https"
    return False


async def create_login_session(db: AsyncSession, user: User, response: Response, request: Request | None = None, auth_method: str = "local") -> str:
    policy = await get_security_policy(db)
    raw_token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    expires = utcnow() + timedelta(hours=policy.session_absolute_hours)
    db.add(LoginSession(id=token_hash(raw_token), user_id=user.id, csrf_token=csrf, expires_at=expires, auth_method=auth_method, ip_address=request.client.host if request and request.client else None, user_agent=(request.headers.get("user-agent") or "")[:500] if request else None))
    await db.commit()
    secure = cookies_are_secure(request)
    max_age = policy.session_absolute_hours * 3600
    response.set_cookie(SESSION_COOKIE, raw_token, httponly=True, secure=secure, samesite="lax", max_age=max_age, path="/")
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, secure=secure, samesite="lax", max_age=max_age, path="/")
    return csrf


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


#: Paths a principal may still reach while forced to change their password. Everything else is
#: refused, so the requirement is a real server-side wall rather than a screen the UI happens to
#: show — a scripted client cannot skip it.
_PASSWORD_CHANGE_ALLOWLIST = frozenset({
    "/api/auth/me",
    "/api/auth/config",
    "/api/auth/change-password",
    "/api/auth/logout",
    "/api/health",
    "/health",
    "/healthz",
    "/readyz",
})

#: A principal with zero effective permissions — the `noaccess` role, or no role at all — is
#: blocked from every path except this minimal set. Without it, `noaccess` would only hide UI and
#: an auto-provisioned SSO user could still call the API directly.
_NO_ACCESS_ALLOWLIST = frozenset({
    "/api/auth/me",
    "/api/auth/config",
    "/api/auth/logout",
    "/api/auth/change-password",
    "/api/health",
    "/health",
    "/healthz",
    "/readyz",
})


#: How stale ``last_seen_at`` may get before a request refreshes it. Writing it on every request
#: puts a write transaction in front of every read, and on SQLite's single writer that serializes
#: the whole API behind the scheduler's own writes. The idle timeout is measured in minutes, so
#: refreshing at most this often costs it nothing.
_LAST_SEEN_REFRESH_SECONDS = 30


async def current_session(request: Request, db: AsyncSession = Depends(get_db)) -> tuple[LoginSession, User]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    record = await db.scalar(select(LoginSession).where(LoginSession.id == token_hash(raw)))
    now = utcnow()
    if not record or record.revoked_at is not None or record.expires_at.replace(tzinfo=record.expires_at.tzinfo or now.tzinfo) <= now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    policy = await get_security_policy(db)
    last_seen = record.last_seen_at.replace(tzinfo=record.last_seen_at.tzinfo or now.tzinfo)
    if last_seen + timedelta(minutes=policy.session_idle_minutes) <= now:
        record.revoked_at = now
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired due to inactivity")
    user = await db.get(User, record.user_id)
    if not user or user.disabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User unavailable")
    # Resolved once here so has_permission can stay synchronous for its many call sites.
    from .access import cache_permissions, effective_permissions

    granted = await effective_permissions(db, user)
    cache_permissions(user, granted)

    path = request.url.path
    if user.must_change_password and path not in _PASSWORD_CHANGE_ALLOWLIST:
        raise HTTPException(status_code=403, detail="Change your password before using the application")
    if not granted and path not in _NO_ACCESS_ALLOWLIST:
        raise HTTPException(status_code=403, detail="This account has no access. Ask an administrator to assign a role.")

    # Always refresh at least twice within the idle window, so a short window still behaves.
    refresh_after = min(_LAST_SEEN_REFRESH_SECONDS, max(policy.session_idle_minutes * 30, 1))
    pending = bool(db.new or db.deleted or db.dirty)
    if (now - last_seen).total_seconds() >= refresh_after:
        record.last_seen_at = now
        pending = True
    if pending:
        await db.commit()
    return record, user

async def current_user(auth: tuple[LoginSession, User] = Depends(current_session)) -> User:
    return auth[1]


async def require_admin(user: User = Depends(current_user)) -> User:
    if not has_permission(user, "users.manage"):
        raise HTTPException(status_code=403, detail="Administrator permission required")
    return user


def has_permission(user: User, permission: str) -> bool:
    from .access import cached_permissions

    granted = cached_permissions(user)
    if granted is None:
        granted = ROLE_PERMISSIONS.get(user.role, set())
    return "*" in granted or permission in granted


def require_permission(permission: str) -> Callable:
    async def dependency(user: User = Depends(current_user)) -> User:
        if not has_permission(user, permission):
            raise HTTPException(status_code=403, detail=f"Permission required: {permission}")
        return user
    return dependency


async def require_csrf(request: Request, auth: tuple[LoginSession, User] = Depends(current_session)) -> User:
    session, user = auth
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get("X-CSRF-Token")
    if not cookie or not header or not secrets.compare_digest(cookie, header) or not secrets.compare_digest(header, session.csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    _assert_same_origin(request)
    return user


def _assert_same_origin(request: Request) -> None:
    """Reject a cookie-authenticated write that a foreign page initiated.

    The token check above already stops classic CSRF. This is defence in depth for the case where
    a token leaks: a browser always attaches Origin (or at least Referer) to a cross-site write, so
    a mismatch means the request did not come from our own page. Requests with neither header are
    allowed because they cannot come from a browser form or fetch — that is a scripted API client.
    """
    origin = request.headers.get("origin") or ""
    referer = request.headers.get("referer") or ""
    candidate = origin or referer
    if not candidate:
        return
    parsed = urlparse(candidate)
    source = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    allowed = set(get_settings().return_origins)
    allowed.add(get_settings().base_url)
    # The deployment is reached on its own hostname, so the request's own host always counts.
    allowed.add(f"{request.url.scheme}://{request.url.netloc}".rstrip("/"))
    if source not in allowed:
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
