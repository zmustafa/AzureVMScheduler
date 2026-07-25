from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import (
    ROLE_PERMISSIONS,
    SESSION_COOKIE,
    bootstrap_access,
    bootstrap_admin,
    clear_session_cookies,
    create_login_session,
        get_identity_provider,
        get_security_policy,
        has_permission,
    current_user,
    hash_password,
    needs_rehash,
    require_admin,
    require_csrf,
    require_permission,
    token_hash,
    validate_password,
    verify_password,
)
from .access import cached_permissions
from .access_routes import router as access_router
from .audit import audit
from .azure import discover, list_virtual_machines, read_power_states, resolve_vm_names
from .backup import BackupDocumentError, SECTIONS as BACKUP_SECTIONS, apply_import, build_export, reset_estate
from .demo import demo_status, load_demo_estate, remove_demo_estate
from .config import get_settings
from .connections import decrypt_value, delete_connection, encrypt_value, get_connection, list_connections, resolve_enabled_connection, set_default, update_connection_status, upsert_connection
from .connectors.base import sanitize_detail
from .connectors.registry import (
    delete_connector,
    get_connector,
    list_connectors,
    send_via_connector,
    test_connector,
    type_metadata,
    update_connector_status,
    upsert_connector,
)
from .csv_import import validate_csv
from .database import engine, get_db, initialize_database, ping_database
from .delivery import delivery_service
from .hierarchy import (
    GroupTree,
    assert_move_allowed,
    assert_parent_allowed,
    assert_unique_sibling_name,
    child_path,
    effective_connection_id,
    ensure_group_path,
    is_stop_protected,
    load_schedule_index,
    load_tree,
    next_sequence,
    recompute_subtree,
    resolve_schedule_vms,
)
from .models import AuditLog, Group, IdentityProvider, ImportBatch, LoginSession, NotificationDelivery, NotificationEvent, NotificationRule, Schedule, ScheduleRun, SecurityPolicy, User, VirtualMachine, VmAttempt, new_id, utcnow
from .notifications import EVENT_TYPES, publish, unread_count
from .overview import build_overview
from . import ip_lockout, oidc, saml
from .oidc import validate_return_url
from .provisioning import ProvisioningError, provision_sso_user
from .schemas import (
    AttemptView,
    AuditView,
    ChangePasswordRequest,
    ConnectionInput,
    ConnectorInput,
    CsvCommitRequest,
    EstateResetRequest,
    GroupInput,
    GroupMove,
    GroupPatch,
    GroupReorder,
    GroupView,
    IdentityProviderUpdate,
    LoginRequest,
    NotificationDeliveryView,
    NotificationEventView,
    NotificationRuleInput,
    NotificationRuleView,
    PasswordReset,
    RecurrencePreviewInput,
    RunView,
    ScheduleInput,
    SchedulePatch,
    ScheduleView,
    SecurityPolicyUpdate,
    SettingsImportRequest,
    UserCreate,
    UserUpdate,
    VmBulkAction,
    VmBulkAdd,
    VmLookupInput,
    VmPowerActionInput,
    VmPowerScanInput,
    VmNameResolveInput,
    VmPatch,
    VmView,
)
from .recurrence import (
    Recurrence,
    RecurrenceError,
    describe as describe_recurrence,
    one_time_at,
    to_cron as to_cron_expression,
    upcoming as upcoming_occurrences,
    validate_cron,
)
from .recurrence import next_occurrence as recurrence_next
from .recurrence import validate as recurrence_validate
from .scheduling import SchedulerService, parse_schedule_time, recurrence_of, resolve_default_timezone, trigger_adhoc_run, trigger_schedule_run
from .templating import build_message
from .validation import normalize_resource_id, parse_subscription_id, parse_vm_resource_id, validate_timezone


#: Image tag, injected at build time. "dev" for a local checkout.
APP_VERSION = os.environ.get("APP_VERSION", "dev")

scheduler = SchedulerService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await initialize_database()
    await bootstrap_admin()
    await bootstrap_access()
    await delivery_service.start()
    await scheduler.start()
    yield
    await scheduler.stop()
    await delivery_service.stop()
    await engine.dispose()


app = FastAPI(title="Azure VM Scheduler API", version="0.1.0", lifespan=lifespan)
app.include_router(access_router)


#: Sent on every response. The app is same-origin (the SPA is served by this process), so the
#: policy can be strict: no framing, no referrer leakage, and scripts only from ourselves.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        # Vite injects inline styles and Tailwind emits a style element at runtime.
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    ),
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    # Behind Azure Container Apps ingress TLS is terminated at the edge, so this process sees plain
    # HTTP; the forwarded protocol is the only way to know the browser used HTTPS.
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    over_https = request.url.scheme == "https" or (get_settings().trust_forwarded_headers and forwarded_proto == "https")
    if over_https:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def user_view(user: User) -> dict[str, Any]:
    granted = cached_permissions(user)
    if granted is None:
        granted = ROLE_PERMISSIONS.get(user.role, set())
    return {"id": user.id, "username": user.username, "email": user.email, "role": user.role, "auth_source": user.auth_source, "must_change_password": user.must_change_password, "disabled": user.disabled, "is_break_glass": user.is_break_glass, "permissions": ["*"] if "*" in granted else sorted(granted)}


def policy_view(policy: SecurityPolicy) -> dict[str, Any]:
    return {"local_login_enabled": policy.local_login_enabled, "min_length": policy.password_min_length, "require_upper": policy.password_require_upper, "require_lower": policy.password_require_lower, "require_number": policy.password_require_number, "require_symbol": policy.password_require_symbol, "lockout_attempts": policy.lockout_attempts, "lockout_minutes": policy.lockout_minutes, "session_idle_minutes": policy.session_idle_minutes, "session_absolute_hours": policy.session_absolute_hours, "schedule_missed_grace_seconds": policy.schedule_missed_grace_seconds, "default_timezone": resolve_default_timezone(policy)}


async def connection_labels() -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in await list_connections(public=True)}


def connection_fields(labels: dict[str, dict[str, Any]], connection_id: str | None) -> dict[str, Any]:
    if not connection_id:
        return {"connection_name": None, "connection_tenant_id": None}
    found = labels.get(connection_id)
    if not found:
        return {"connection_name": "Unknown connection", "connection_tenant_id": None}
    return {"connection_name": found.get("display_name") or "Unknown connection", "connection_tenant_id": found.get("tenant_id") or None}


def group_payload(tree: GroupTree, group: Group, labels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    inherited = next((node.azure_connection_id for node in tree.chain(group.id) if node.azure_connection_id), None)
    effective = group.azure_connection_id or inherited
    return {
        **GroupView.model_validate(group).model_dump(mode="json"),
        "kind": group.kind,
        "name_path": tree.name_path(group.id),
        "effective_enabled": tree.is_active(group.id),
        **connection_fields(labels, group.azure_connection_id),
        "effective_connection_id": effective,
        "effective_connection_name": connection_fields(labels, effective)["connection_name"],
        "effective_connection_tenant_id": connection_fields(labels, effective)["connection_tenant_id"],
        "connection_inherited": bool(effective and not group.azure_connection_id),
    }


def vm_payload(tree: GroupTree, vm: VirtualMachine, labels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    resolved = effective_connection_id(tree, vm)
    inherited = connection_fields(labels, resolved)
    return {
        **VmView.model_validate(vm).model_dump(mode="json"),
        "group_path": tree.name_path(vm.group_id),
        "effective_connection_id": resolved,
        **connection_fields(labels, vm.azure_connection_id),
        "effective_connection_name": inherited["connection_name"],
        # The portal deep link needs the tenant the VM actually resolves to, not just its own override.
        "effective_connection_tenant_id": inherited["connection_tenant_id"],
        # Resolved here because protection is inherited: the UI cannot work it out from never_stop alone.
        "stop_protected": is_stop_protected(tree, vm),
    }


def schedule_payload(tree: GroupTree, schedule: Schedule, labels: dict[str, dict[str, Any]], vm_names: dict[str, str] | None = None) -> dict[str, Any]:
    target_label = tree.name_path(schedule.target_id) if schedule.target_type == "group" else (vm_names or {}).get(schedule.target_id, "")
    return {
        **ScheduleView.model_validate(schedule).model_dump(mode="json"),
        "target_label": target_label or "Unknown target",
        **connection_fields(labels, schedule.azure_connection_id),
    }


def attempt_payload(attempt: VmAttempt, labels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {**AttemptView.model_validate(attempt).model_dump(mode="json"), **connection_fields(labels, attempt.connection_id)}


def run_payload(run: ScheduleRun, labels: dict[str, dict[str, Any]], connection_id: str | None = None) -> dict[str, Any]:
    return {**RunView.model_validate(run).model_dump(mode="json"), **connection_fields(labels, connection_id)}


async def load_group(db: AsyncSession, group_id: str) -> Group:
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


async def load_vm(db: AsyncSession, vm_id: str) -> VirtualMachine:
    vm = await db.get(VirtualMachine, vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="Virtual machine not found")
    return vm


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=value.tzinfo or timezone.utc) if value else None


def _safe_error(exc: Exception) -> str:
    text = re.sub(r"(?i)(client_secret|access_token|authorization|password)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", str(exc))
    return text[:300] or "Operation failed"


def _sorted(statement, columns: dict[str, Any], sort: str | None, direction: str, default: str):
    """Order a listing by one of a fixed set of columns; unknown keys fall back to the default."""
    column = columns.get(sort or "", columns[default])
    ordered = statement.order_by(column.desc() if direction == "desc" else column.asc())
    tiebreak = columns[default]
    return ordered if column is tiebreak else ordered.order_by(tiebreak.asc())


def _ci(column):
    """SQLite sorts text with a binary collation, so 'RG-A' would come before 'rg-a'."""
    return func.lower(column)


VM_SORT_COLUMNS = {
    "display_name": _ci(VirtualMachine.display_name),
    "vm_name": _ci(VirtualMachine.vm_name),
    "resource_group": _ci(VirtualMachine.resource_group),
    "subscription_id": _ci(VirtualMachine.subscription_id),
    "enabled": VirtualMachine.enabled,
    "created_at": VirtualMachine.created_at,
}
SCHEDULE_SORT_COLUMNS = {
    "name": _ci(Schedule.name),
    "action": _ci(Schedule.action),
    "schedule_type": _ci(Schedule.schedule_type),
    "start_time": Schedule.start_time,
    "next_run_at": Schedule.next_run_at,
    "status": _ci(Schedule.status),
    "enabled": Schedule.enabled,
}
RUN_SORT_COLUMNS = {
    "schedule_name": _ci(ScheduleRun.schedule_name),
    "scheduled_for": ScheduleRun.scheduled_for,
    "started_at": ScheduleRun.started_at,
    "status": _ci(ScheduleRun.status),
    "mode": _ci(ScheduleRun.mode),
    "trigger": _ci(ScheduleRun.trigger),
    "created_at": ScheduleRun.created_at,
}
DELIVERY_SORT_COLUMNS = {
    "created_at": NotificationDelivery.created_at,
    "status": _ci(NotificationDelivery.status),
    "connector_label": _ci(NotificationDelivery.connector_label),
    "attempts": NotificationDelivery.attempts,
}
SortDirection = Literal["asc", "desc"]


def _vm_filter(statement, tree: GroupTree, q: str | None, group_id: str | None, enabled: bool | None, connection_id: str | None):
    """Shared filter for the VM listing and its CSV export, so both always agree."""
    if q:
        statement = statement.where(VirtualMachine.vm_resource_id.ilike(f"%{q}%") | VirtualMachine.display_name.ilike(f"%{q}%"))
    if group_id:
        statement = statement.where(VirtualMachine.group_id.in_(list(tree.subtree_ids(group_id) or {group_id})))
    if enabled is not None:
        statement = statement.where(VirtualMachine.enabled == enabled)
    if connection_id:
        statement = statement.where(VirtualMachine.azure_connection_id == connection_id)
    return statement


def _preview_token(rows: list[dict[str, Any]]) -> str:
    payload = {"iat": int(utcnow().timestamp()), "rows": rows}
    return encrypt_value(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _validate_preview_token(token: str, rows: list[dict[str, Any]]) -> None:
    try:
        payload = json.loads(decrypt_value(token))
        if int(utcnow().timestamp()) - int(payload["iat"]) > get_settings().import_preview_ttl_seconds:
            raise ValueError("expired")
        expected = json.dumps(payload["rows"], sort_keys=True, separators=(",", ":"))
        actual = json.dumps(rows, sort_keys=True, separators=(",", ":"))
        if not secrets.compare_digest(expected, actual):
            raise ValueError("mismatch")
    except Exception as exc:
        raise HTTPException(status_code=409, detail="Import preview token is invalid, expired, or does not match these rows") from exc


async def assert_target_exists(db: AsyncSession, target_type: str, target_id: str) -> None:
    model = Group if target_type == "group" else VirtualMachine
    if not await db.get(model, target_id):
        raise HTTPException(status_code=422, detail=f"Schedule target {target_type} does not exist")


def apply_schedule_values(schedule: Schedule, payload: ScheduleInput | SchedulePatch, missed_grace_seconds: int = 300, default_timezone: str = "America/New_York") -> None:
    values = {key: value for key, value in payload.model_dump(exclude_unset=True).items() if not (key == "timezone" and value is None)}
    prospective_zone = values.get("timezone") or schedule.timezone or default_timezone
    validate_timezone(prospective_zone)
    values["timezone"] = prospective_zone
    # A Schedule that has not been flushed yet has enabled=None rather than the column default, so
    # falling back to it directly would quietly park a brand-new schedule with no next run.
    current_enabled = True if schedule.enabled is None else schedule.enabled
    prospective_enabled = values.get("enabled", current_enabled)

    def prospective(key: str, fallback: Any = "") -> Any:
        return values.get(key, getattr(schedule, key, None) or fallback)

    recurrence = Recurrence(
        schedule_type=prospective("schedule_type"),
        timezone=prospective_zone,
        start_time=prospective("start_time"),
        cron_expression=prospective("cron_expression"),
        weekday=values.get("weekday", schedule.weekday),
        start_date=prospective("start_date"),
        end_date=prospective("end_date"),
        run_limit=values.get("run_limit", schedule.run_limit),
        run_count=schedule.run_count or 0,
    )
    try:
        recurrence_validate(recurrence)
    except RecurrenceError as exc:
        raise ValueError(str(exc)) from exc
    if recurrence.schedule_type == "one_time":
        # A one-time start may be slightly in the past; the missed-run grace decides how slightly.
        next_run = one_time_at(recurrence)
        if next_run < utcnow() - timedelta(seconds=missed_grace_seconds):
            raise ValueError("one_time start_time is in the past beyond the configured grace period")
    else:
        next_run = recurrence_next(recurrence)
    if recurrence.schedule_type == "cron":
        values["cron_expression"] = validate_cron(recurrence.cron_expression)

    for key, value in values.items():
        setattr(schedule, key, value)
    schedule.next_run_at = next_run if prospective_enabled else None
    schedule.status = ("scheduled" if next_run else "completed") if prospective_enabled else "disabled"


async def resolve_schedule_connection(connection_id: str | None) -> str:
    try:
        connection = await resolve_enabled_connection(connection_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return str(connection["id"])


async def reject_duplicate_schedule(db: AsyncSession, schedule: Schedule) -> None:
    # Action is part of the identity: a ring legitimately has both a start and a stop wave, and they
    # may even share a start time if an operator wants them to. Cron schedules share an empty
    # start_time, so the expression itself has to be compared too.
    statement = select(Schedule.id).where(
        Schedule.schedule_type == schedule.schedule_type,
        Schedule.start_time == schedule.start_time,
        Schedule.cron_expression == (schedule.cron_expression or ""),
        Schedule.weekday.is_not_distinct_from(schedule.weekday),
        Schedule.target_type == schedule.target_type,
        Schedule.target_id == schedule.target_id,
        Schedule.action == schedule.action,
    )
    if schedule.id:
        statement = statement.where(Schedule.id != schedule.id)
    if await db.scalar(statement.limit(1)):
        raise HTTPException(status_code=409, detail="A schedule with the same action, recurrence, and target already exists")


@app.get("/health")
@app.get("/healthz")
@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "azureops", "server_time": utcnow().isoformat()}


@app.get("/readyz")
@app.get("/api/ready")
async def ready():
    """Liveness says the process is up; this says the database is actually reachable."""
    try:
        await ping_database()
    except Exception as exc:  # pragma: no cover - depends on a broken database
        return JSONResponse(status_code=503, content={"status": "unavailable", "detail": type(exc).__name__})
    return {"status": "ready", "version": APP_VERSION}


@app.get("/api/meta")
async def meta() -> dict[str, str]:
    return {"name": get_settings().app_name, "version": APP_VERSION, "environment": get_settings().environment}


@app.post("/api/auth/login")
async def login(payload: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    policy = await get_security_policy(db)
    ip = ip_lockout.client_ip(request)
    # Per-IP check first: an attacker spraying many usernames from one source is stopped before
    # any account's own counter is touched.
    blocked_for = await ip_lockout.check(db, policy, ip)
    if blocked_for is not None:
        raise HTTPException(status_code=429, detail=f"Too many sign-in attempts. Try again in {blocked_for} seconds.")
    user = await db.scalar(select(User).where(func.lower(User.username) == payload.username.lower()))
    now = utcnow()
    if user and not policy.local_login_enabled and not user.is_break_glass:
        raise HTTPException(status_code=403, detail="Local login is disabled")
    locked_until = _aware(user.locked_until) if user else None
    if locked_until is not None and locked_until > now:
        raise HTTPException(status_code=423, detail="Account is temporarily locked")
    valid = bool(user and user.password_hash and verify_password(user.password_hash, payload.password))
    if not user or user.disabled or not valid:
        if user and not user.disabled:
            user.failed_login_count += 1
            if user.failed_login_count >= policy.lockout_attempts:
                user.locked_until = now + timedelta(minutes=policy.lockout_minutes)
                user.failed_login_count = 0
        # Counted for an unknown username too, so probing for valid accounts is throttled.
        await ip_lockout.record_failure(db, policy, ip)
        await db.commit()
        raise HTTPException(status_code=401, detail="Invalid username or password")
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    # Transparently upgrade a hash made with weaker Argon2 parameters.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
    await ip_lockout.clear(db, ip)
    await create_login_session(db, user, response, request, "local")
    audit(db, user, "auth.login", "user", user.id)
    await db.commit()
    return {"user": user_view(user), "password_policy": policy_view(policy)}


@app.get("/api/auth/config")
async def auth_config(db: AsyncSession = Depends(get_db)):
    """What the sign-in page should offer. Public: no session required."""
    policy = await get_security_policy(db)
    providers = (await db.scalars(select(IdentityProvider).where(IdentityProvider.enabled.is_(True)).order_by(IdentityProvider.name))).all()
    usable = [item for item in providers if (item.type == "saml" and saml.is_configured(item)) or (item.type != "saml" and oidc.is_configured(item))]
    return {
        "local_login_enabled": policy.local_login_enabled,
        "providers": [
            {
                "id": item.id,
                "name": item.name,
                "type": item.type,
                "button_label": item.button_label or f"Sign in with {item.name}",
                "start_url": f"/api/auth/{'saml' if item.type == 'saml' else 'oidc'}/{item.id}/login",
            }
            for item in usable
        ],
        # Retained so an older bundle that has not reloaded still renders its Entra button.
        "entra": {"enabled": any(item.type == "entra" for item in usable)},
    }


async def _enabled_provider(db: AsyncSession, idp_id: str, kind: str) -> IdentityProvider:
    provider = await db.get(IdentityProvider, idp_id)
    if not provider or not provider.enabled:
        raise HTTPException(status_code=404, detail="This sign-in provider is not available")
    if (provider.type == "saml") != (kind == "saml"):
        raise HTTPException(status_code=404, detail="This sign-in provider is not available")
    return provider


async def _finish_sso_login(
    db: AsyncSession,
    request: Request,
    provider: IdentityProvider,
    identity: dict[str, Any],
    return_url: str,
) -> RedirectResponse:
    """Shared tail of every SSO flow: provision, start a session, audit, redirect."""
    try:
        user = await provision_sso_user(
            db,
            provider,
            external_id=identity["external_id"],
            email=identity.get("email", ""),
            display_name=identity.get("display_name", ""),
            groups=identity.get("groups") or [],
            email_verified=bool(identity.get("email_verified", True)),
        )
    except ProvisioningError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    response = RedirectResponse(validate_return_url(return_url), status_code=302)
    await create_login_session(db, user, response, request, provider.type)
    audit(db, user, "auth.sso_login", "user", user.id, {"provider": provider.name, "type": provider.type})
    await db.commit()
    return response


@app.get("/api/auth/oidc/{idp_id}/login")
async def oidc_login(idp_id: str, request: Request, return_url: str | None = None, db: AsyncSession = Depends(get_db)):
    provider = await _enabled_provider(db, idp_id, "oidc")
    target = await oidc.build_authorize_url(provider, oidc.callback_url(request, idp_id), return_url)
    return RedirectResponse(target, status_code=302)


@app.get("/api/auth/oidc/{idp_id}/callback", name="oidc_callback")
async def oidc_callback(idp_id: str, request: Request, code: str, state: str, db: AsyncSession = Depends(get_db)):
    provider = await _enabled_provider(db, idp_id, "oidc")
    state_payload = oidc.read_state(state)
    if state_payload.get("idp") != provider.id:
        raise HTTPException(status_code=400, detail="Sign-in state does not match this provider")
    claims = await oidc.exchange_and_validate(provider, code, oidc.callback_url(request, idp_id), state_payload)
    identity = oidc.extract_identity(claims, provider.config_json or {})
    return await _finish_sso_login(db, request, provider, identity, state_payload.get("return_url") or "/")


@app.get("/api/auth/saml/{idp_id}/metadata")
async def saml_metadata(idp_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Service-provider metadata to hand to the identity provider. Public by design."""
    provider = await _enabled_provider(db, idp_id, "saml")
    base = saml.public_base_url(request)
    return Response(content=saml.sp_metadata(base, provider.id), media_type="application/xml")


@app.get("/api/auth/saml/{idp_id}/login")
async def saml_login(idp_id: str, request: Request, return_url: str | None = None, db: AsyncSession = Depends(get_db)):
    provider = await _enabled_provider(db, idp_id, "saml")
    base = saml.public_base_url(request)
    target = saml.build_authn_request(provider, base, validate_return_url(return_url))
    return RedirectResponse(target, status_code=302)


@app.post("/api/auth/saml/{idp_id}/acs")
async def saml_acs(idp_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Assertion Consumer Service: the IdP posts the signed assertion back here."""
    provider = await _enabled_provider(db, idp_id, "saml")
    form = await request.form()
    base = saml.public_base_url(request)
    identity, return_url = saml.validate_response(
        provider,
        str(form.get("SAMLResponse") or ""),
        str(form.get("RelayState") or ""),
        base,
    )
    return await _finish_sso_login(db, request, provider, identity, return_url)


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    raw_token = request.cookies.get(SESSION_COOKIE, "")
    await db.execute(delete(LoginSession).where(LoginSession.id == token_hash(raw_token)))
    audit(db, user, "auth.logout", "user", user.id)
    await db.commit()
    clear_session_cookies(response)
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    policy = await get_security_policy(db)
    return {"user": user_view(user), "password_policy": policy_view(policy)}


@app.post("/api/auth/change-password")
async def change_password(payload: ChangePasswordRequest, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not user.password_hash or not verify_password(user.password_hash, payload.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    policy = await get_security_policy(db)
    errors = validate_password(payload.new_password, policy)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    audit(db, user, "auth.password_changed", "user", user.id)
    await db.commit()
    return {"ok": True, "user": user_view(user)}


@app.get("/api/settings/general")
async def general_settings(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    policy = await get_security_policy(db)
    return {"app_name": settings.app_name, "environment": settings.environment, "real_azure_starts_enabled": settings.enable_real_azure_starts, "real_azure_stops_enabled": settings.enable_real_azure_stops, "default_timezone": resolve_default_timezone(policy), "server_time": utcnow().isoformat(), "password_policy": policy_view(policy)}


@app.put("/api/settings/general")
async def general_settings_update(payload: dict[str, Any], user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator permission required")
    policy = await get_security_policy(db)
    if "default_timezone" in payload:
        try:
            policy.default_timezone = validate_timezone(str(payload["default_timezone"]))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(db, user, "settings.general_updated", "security_policy", "1", {"default_timezone": policy.default_timezone})
    await db.commit()
    return {"default_timezone": resolve_default_timezone(policy)}


# Users, roles, access groups, sessions, sign-in policy and identity providers now live on the
# access control router (/api/access). See app/access_routes.py.


# -- settings backup, restore and estate reset -------------------------


@app.get("/api/admin/export")
async def settings_export(user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Portable settings document. Secrets are deliberately absent and must be re-entered after import."""
    document = await build_export(db, await list_connections(public=True), await list_connectors(public=True), app.version)
    audit(db, user, "settings.exported", "backup", None, {"groups": len(document["groups"]), "vms": len(document["virtual_machines"]), "schedules": len(document["schedules"])})
    await db.commit()
    filename = f"azure-vm-scheduler-settings-{utcnow().date().isoformat()}.json"
    return Response(
        content=json.dumps(document, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@app.get("/api/admin/import/sections")
async def settings_import_sections(user: User = Depends(require_admin)):
    return {"sections": list(BACKUP_SECTIONS)}


async def _run_import(payload: SettingsImportRequest, user: User, db: AsyncSession, dry_run: bool) -> dict[str, Any]:
    try:
        summary = await apply_import(
            db,
            payload.document,
            mode=payload.mode,
            sections=payload.sections,
            user=user,
            connections=await list_connections(public=True),
            connectors=await list_connectors(public=True),
            dry_run=dry_run,
        )
    except BackupDocumentError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        await db.rollback()
        raise
    return summary


@app.post("/api/admin/import/preview")
async def settings_import_preview(payload: SettingsImportRequest, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator permission required")
    summary = await _run_import(payload, user, db, dry_run=True)
    await db.rollback()  # a preview never persists anything
    return summary


@app.post("/api/admin/import")
async def settings_import(payload: SettingsImportRequest, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator permission required")
    summary = await _run_import(payload, user, db, dry_run=False)
    audit(db, user, "settings.imported", "backup", None, {"mode": payload.mode, "sections": payload.sections or list(BACKUP_SECTIONS), "created": summary["created"], "skipped": summary["skipped"], "failed": summary["failed"]})
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return summary


@app.post("/api/admin/reset-estate")
async def estate_reset(payload: EstateResetRequest, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    """Destroys every application, ring, VM, schedule and run. Identity, audit and credentials survive."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator permission required")
    if payload.confirm != "DELETE":
        raise HTTPException(status_code=422, detail="Type DELETE to confirm removing every application, virtual machine and schedule")
    removed = await reset_estate(db)
    audit(db, user, "estate.reset", "backup", None, removed)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return removed


@app.get("/api/admin/demo-data")
async def demo_data_status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """What the sample estate currently looks like, so the Settings card can describe the action."""
    if not has_permission(user, "groups.write"):
        raise HTTPException(status_code=403, detail="Permission required: groups.write")
    return await demo_status(db)


@app.post("/api/admin/demo-data")
async def demo_data_load(user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    """Create the Zava sample estate. Applications that already exist by name are left alone."""
    if not has_permission(user, "groups.write"):
        raise HTTPException(status_code=403, detail="Permission required: groups.write")
    counts = await load_demo_estate(db, created_by=user.id)
    audit(db, user, "demo.load", "backup", None, counts)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return counts | {"status": await demo_status(db)}


@app.delete("/api/admin/demo-data")
async def demo_data_remove(user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    """Remove only what the demo loader created; real applications are never touched."""
    if not has_permission(user, "groups.write"):
        raise HTTPException(status_code=403, detail="Permission required: groups.write")
    counts = await remove_demo_estate(db)
    audit(db, user, "demo.remove", "backup", None, counts)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return counts | {"status": await demo_status(db)}


@app.get("/api/dashboard")
async def dashboard(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    policy = await get_security_policy(db)
    labels = await connection_labels()
    tree = await load_tree(db)
    now = utcnow()
    counts = {
        "schedule_count": await db.scalar(select(func.count()).select_from(Schedule)),
        "enabled_count": await db.scalar(select(func.count()).select_from(Schedule).where(Schedule.enabled.is_(True))),
        "group_count": await db.scalar(select(func.count()).select_from(Group)),
        "application_count": await db.scalar(select(func.count()).select_from(Group).where(Group.depth == 0)),
        "ring_count": await db.scalar(select(func.count()).select_from(Group).where(Group.depth > 0)),
        "vm_count": await db.scalar(select(func.count()).select_from(VirtualMachine)),
        "enabled_vm_count": await db.scalar(select(func.count()).select_from(VirtualMachine).where(VirtualMachine.enabled.is_(True))),
        "failed_attempts": await db.scalar(select(func.count()).select_from(VmAttempt).where(VmAttempt.status.in_(["failed", "timed_out"]))),
        "running_runs": await db.scalar(select(func.count()).select_from(ScheduleRun).where(ScheduleRun.finished_at.is_(None))),
        "failed_runs": await db.scalar(select(func.count()).select_from(ScheduleRun).where(ScheduleRun.status.in_(["failed", "partially_failed", "timed_out"]))),
        "late_start_count": await db.scalar(select(func.count()).select_from(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run_at.is_not(None), Schedule.next_run_at < now - timedelta(seconds=policy.schedule_missed_grace_seconds))),
    }
    next_schedule = await db.scalar(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run_at.is_not(None)).order_by(Schedule.next_run_at).limit(1))
    recent_attempts = (await db.scalars(select(VmAttempt).order_by(VmAttempt.claimed_at.desc()).limit(8))).all()
    recent_runs = (await db.scalars(select(ScheduleRun).order_by(ScheduleRun.created_at.desc()).limit(8))).all()
    return {
        **counts,
        "next_schedule": schedule_payload(tree, next_schedule, labels) if next_schedule else None,
        "recent_attempts": [attempt_payload(item, labels) for item in recent_attempts],
        "recent_runs": [run_payload(item, labels) for item in recent_runs],
    }


@app.get("/api/overview")
async def overview(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Windowed operations overview: KPIs with trend, readiness checks, coverage gaps and the rollout plan."""
    window_end = (to or utcnow()).astimezone(timezone.utc)
    window_start = (from_ or window_end - timedelta(days=1)).astimezone(timezone.utc)
    if window_end <= window_start:
        raise HTTPException(status_code=422, detail="to must be later than from")
    if window_end - window_start > timedelta(days=180):
        raise HTTPException(status_code=422, detail="Overview windows are limited to 180 days")
    settings = get_settings()
    return await build_overview(
        db,
        window_start,
        window_end,
        connections=await list_connections(public=True),
        real_starts_enabled=settings.enable_real_azure_starts,
        real_stops_enabled=settings.enable_real_azure_stops,
        policy=await get_security_policy(db),
        monitor_timeout_seconds=settings.vm_monitor_timeout_seconds,
    )


# -- groups ------------------------------------------------------------


@app.get("/api/groups")
async def groups_list(shape: str = Query("tree", pattern="^(tree|flat)$"), user: User = Depends(require_permission("groups.read")), db: AsyncSession = Depends(get_db)):
    tree = await load_tree(db)
    labels = await connection_labels()
    vm_counts = dict((await db.execute(select(VirtualMachine.group_id, func.count()).group_by(VirtualMachine.group_id))).all())
    schedule_counts = dict((await db.execute(select(Schedule.target_id, func.count()).where(Schedule.target_type == "group").group_by(Schedule.target_id))).all())
    group_next = dict((await db.execute(select(Schedule.target_id, func.min(Schedule.next_run_at)).where(Schedule.target_type == "group", Schedule.enabled.is_(True), Schedule.next_run_at.is_not(None)).group_by(Schedule.target_id))).all())
    nodes = sorted(tree.by_id.values(), key=lambda item: (item.depth, item.sequence, item.name.lower()))
    items = []
    for group in nodes:
        subtree = tree.subtree_ids(group.id)
        subtree_runs = [group_next[item] for item in subtree if group_next.get(item)]
        items.append({
            **group_payload(tree, group, labels),
            "vm_count": vm_counts.get(group.id, 0),
            "subtree_vm_count": sum(vm_counts.get(item, 0) for item in subtree),
            "schedule_count": schedule_counts.get(group.id, 0),
            "subtree_schedule_count": sum(schedule_counts.get(item, 0) for item in subtree),
            "next_run_at": _aware(group_next.get(group.id)),
            "subtree_next_run_at": _aware(min(subtree_runs)) if subtree_runs else None,
            "children": [],
        })
    if shape == "flat":
        return sorted(items, key=lambda item: (item["path"],))
    by_id = {item["id"]: item for item in items}
    roots = []
    for item in items:
        parent = by_id.get(item["parent_id"] or "")
        (parent["children"] if parent else roots).append(item)
    return roots


@app.post("/api/groups", status_code=201)
async def group_create(payload: GroupInput, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "groups.write"):
        raise HTTPException(status_code=403, detail="Permission required: groups.write")
    parent = await db.get(Group, payload.parent_id) if payload.parent_id else None
    if payload.parent_id and not parent:
        raise HTTPException(status_code=422, detail="Parent group not found")
    try:
        assert_parent_allowed(parent)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        await assert_unique_sibling_name(db, payload.parent_id, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    group = Group(id=new_id(), parent_id=payload.parent_id, name=payload.name.strip(), description=payload.description, azure_connection_id=payload.azure_connection_id, enabled=payload.enabled, sequence=await next_sequence(db, payload.parent_id), created_by=user.id)
    db.add(group)
    await db.flush()
    try:
        await recompute_subtree(db, group)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(db, user, "group.created", "group", group.id, {"name": group.name, "parent_id": group.parent_id, "kind": group.kind})
    await db.commit()
    tree = await load_tree(db)
    return group_payload(tree, group, await connection_labels())


@app.get("/api/groups/{group_id}")
async def group_detail(group_id: str, user: User = Depends(require_permission("groups.read")), db: AsyncSession = Depends(get_db)):
    group = await load_group(db, group_id)
    tree = await load_tree(db)
    labels = await connection_labels()
    vms = (await db.scalars(select(VirtualMachine).where(VirtualMachine.group_id == group_id))).all()
    schedules = (await db.scalars(select(Schedule).where(Schedule.target_type == "group", Schedule.target_id == group_id))).all()
    return {
        "group": group_payload(tree, group, labels),
        "ancestors": [group_payload(tree, item, labels) for item in reversed(tree.chain(group_id)[1:])],
        "vms": [vm_payload(tree, item, labels) for item in vms],
        "schedules": [schedule_payload(tree, item, labels) for item in schedules],
    }


@app.patch("/api/groups/{group_id}")
async def group_update(group_id: str, payload: GroupPatch, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "groups.write"):
        raise HTTPException(status_code=403, detail="Permission required: groups.write")
    group = await load_group(db, group_id)
    values = payload.model_dump(exclude_unset=True)
    if "name" in values and values["name"]:
        try:
            await assert_unique_sibling_name(db, group.parent_id, values["name"], exclude_id=group.id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        group.name = values["name"].strip()
    for key in ("description", "azure_connection_id", "enabled"):
        if key in values:
            setattr(group, key, values[key])
    audit(db, user, "group.updated", "group", group.id, values)
    await db.commit()
    tree = await load_tree(db)
    return group_payload(tree, group, await connection_labels())


@app.delete("/api/groups/{group_id}")
async def group_delete(group_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "groups.write"):
        raise HTTPException(status_code=403, detail="Permission required: groups.write")
    group = await load_group(db, group_id)
    tree = await load_tree(db)
    subtree = tree.subtree_ids(group_id)
    vm_ids = list((await db.scalars(select(VirtualMachine.id).where(VirtualMachine.group_id.in_(list(subtree))))).all())
    await db.execute(delete(Schedule).where(Schedule.target_type == "group", Schedule.target_id.in_(list(subtree))))
    if vm_ids:
        await db.execute(delete(Schedule).where(Schedule.target_type == "vm", Schedule.target_id.in_(vm_ids)))
    audit(db, user, "group.deleted", "group", group.id, {"name": group.name, "groups_removed": len(subtree), "vms_removed": len(vm_ids)})
    await db.delete(group)
    await db.commit()
    return {"ok": True, "groups_removed": len(subtree), "vms_removed": len(vm_ids)}


@app.post("/api/groups/{group_id}/move")
async def group_move(group_id: str, payload: GroupMove, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "groups.write"):
        raise HTTPException(status_code=403, detail="Permission required: groups.write")
    group = await load_group(db, group_id)
    tree = await load_tree(db)
    try:
        assert_move_allowed(tree, group_id, payload.parent_id)
        await assert_unique_sibling_name(db, payload.parent_id, group.name, exclude_id=group.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    group.parent_id = payload.parent_id
    group.sequence = payload.sequence if payload.sequence is not None else await next_sequence(db, payload.parent_id)
    try:
        await recompute_subtree(db, group)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(db, user, "group.moved", "group", group.id, {"parent_id": group.parent_id, "sequence": group.sequence})
    await db.commit()
    tree = await load_tree(db)
    return group_payload(tree, group, await connection_labels())


@app.post("/api/groups/reorder")
async def groups_reorder(payload: GroupReorder, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "groups.write"):
        raise HTTPException(status_code=403, detail="Permission required: groups.write")
    statement = select(Group).where(Group.parent_id.is_(None)) if payload.parent_id is None else select(Group).where(Group.parent_id == payload.parent_id)
    siblings = {item.id: item for item in (await db.scalars(statement)).all()}
    if set(payload.ordered_ids) != set(siblings):
        raise HTTPException(status_code=422, detail="ordered_ids must list every sibling exactly once")
    for position, item_id in enumerate(payload.ordered_ids):
        siblings[item_id].sequence = position
    audit(db, user, "group.reordered", "group", payload.parent_id, {"count": len(payload.ordered_ids)})
    await db.commit()
    return {"ok": True}


@app.get("/api/groups/{group_id}/vms")
async def group_vms(
    group_id: str,
    recursive: bool = True,
    sort: str | None = None,
    direction: SortDirection = "asc",
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_permission("vms.read")),
    db: AsyncSession = Depends(get_db),
):
    await load_group(db, group_id)
    tree = await load_tree(db)
    labels = await connection_labels()
    scope = tree.subtree_ids(group_id) if recursive else {group_id}
    statement = select(VirtualMachine).where(VirtualMachine.group_id.in_(list(scope)))
    total = await db.scalar(select(func.count()).select_from(statement.subquery()))
    ordered = _sorted(statement, VM_SORT_COLUMNS, sort, direction, "vm_name")
    items = (await db.scalars(ordered.limit(limit).offset(offset))).all()
    return {"items": [vm_payload(tree, item, labels) for item in items], "total": total or 0, "limit": limit, "offset": offset}


@app.post("/api/groups/{group_id}/vms", status_code=201)
async def group_vms_add(group_id: str, payload: VmBulkAdd, response: Response, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "vms.write"):
        raise HTTPException(status_code=403, detail="Permission required: vms.write")
    await load_group(db, group_id)
    created: list[VirtualMachine] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for resource_id in payload.vm_resource_ids:
        normalized = normalize_resource_id(resource_id)
        try:
            parsed = parse_vm_resource_id(resource_id)
        except ValueError as exc:
            errors.append({"vm_resource_id": resource_id, "error": str(exc)})
            continue
        if normalized in seen or await db.scalar(select(VirtualMachine.id).where(VirtualMachine.normalized_resource_id == normalized)):
            errors.append({"vm_resource_id": resource_id, "error": "This VM is already in the inventory"})
            continue
        seen.add(normalized)
        created.append(VirtualMachine(id=new_id(), group_id=group_id, vm_resource_id=resource_id.strip(), normalized_resource_id=normalized, display_name=parsed.vm_name, subscription_id=parsed.subscription_id, resource_group=parsed.resource_group, vm_name=parsed.vm_name, azure_connection_id=payload.azure_connection_id, enabled=payload.enabled, notes=payload.notes, created_by=user.id))
    db.add_all(created)
    audit(db, user, "vm.bulk_added", "group", group_id, {"created": len(created), "rejected": len(errors)})
    await db.commit()
    tree = await load_tree(db)
    labels = await connection_labels()
    # A batch that created nothing is not a 201; report it as a rejected request instead.
    if not created:
        response.status_code = 422
    return {"created": [vm_payload(tree, item, labels) for item in created], "errors": errors}


# -- virtual machines --------------------------------------------------


@app.get("/api/vms")
async def vms_list(
    q: str | None = None,
    group_id: str | None = None,
    enabled: bool | None = None,
    connection_id: str | None = None,
    sort: str | None = None,
    direction: SortDirection = "asc",
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_permission("vms.read")),
    db: AsyncSession = Depends(get_db),
):
    tree = await load_tree(db)
    labels = await connection_labels()
    statement = _vm_filter(select(VirtualMachine), tree, q, group_id, enabled, connection_id)
    total = await db.scalar(select(func.count()).select_from(statement.subquery()))
    ordered = _sorted(statement, VM_SORT_COLUMNS, sort, direction, "vm_name")
    items = (await db.scalars(ordered.limit(limit).offset(offset))).all()
    return {"items": [vm_payload(tree, item, labels) for item in items], "total": total or 0, "limit": limit, "offset": offset}


@app.get("/api/vms/export.csv")
async def vms_export_csv(
    q: str | None = None,
    group_id: str | None = None,
    recursive: bool = True,
    enabled: bool | None = None,
    connection_id: str | None = None,
    sort: str | None = None,
    direction: SortDirection = "asc",
    user: User = Depends(require_permission("vms.read")),
    db: AsyncSession = Depends(get_db),
):
    """The whole filtered inventory as CSV, in the same columns the importer accepts."""
    tree = await load_tree(db)
    labels = await connection_labels()
    statement = select(VirtualMachine)
    if group_id and not recursive:
        statement = statement.where(VirtualMachine.group_id == group_id)
        statement = _vm_filter(statement, tree, q, None, enabled, connection_id)
    else:
        statement = _vm_filter(statement, tree, q, group_id, enabled, connection_id)
    ordered = _sorted(statement, VM_SORT_COLUMNS, sort, direction, "vm_name")
    machines = (await db.scalars(ordered)).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["application", "ring_path", "vm_resource_id", "vm_name", "display_name", "enabled", "never_stop", "notes", "azure_connection"])
    for machine in machines:
        chain = list(reversed(tree.chain(machine.group_id)))
        application = chain[0].name if chain else ""
        ring = chain[1].name if len(chain) > 1 else ""
        connection = connection_fields(labels, machine.azure_connection_id)["connection_name"] or ""
        writer.writerow([application, ring, machine.vm_resource_id, machine.vm_name, machine.display_name, "true" if machine.enabled else "false", "true" if machine.never_stop else "false", machine.notes, connection])

    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="azure-vm-scheduler-vms-{stamp}.csv"'},
    )


@app.post("/api/vms/lookup")
async def vms_lookup(payload: VmLookupInput, user: User = Depends(require_permission("vms.read")), db: AsyncSession = Depends(get_db)):
    """Match pasted VM names (or full resource IDs) against the existing inventory. Local only — never calls Azure."""
    queries = [item.strip() for item in payload.names if item.strip()]
    if not queries:
        raise HTTPException(status_code=422, detail="Provide at least one virtual machine name")
    unique: list[str] = []
    seen: set[str] = set()
    for item in queries:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    if len(unique) > 1000:
        raise HTTPException(status_code=422, detail="Look up at most 1000 names at a time")

    tree = await load_tree(db)
    labels = await connection_labels()
    machines = (await db.scalars(select(VirtualMachine))).all()
    by_key: dict[str, list[VirtualMachine]] = {}
    for machine in machines:
        for key in {machine.vm_name.casefold(), (machine.display_name or "").casefold(), machine.normalized_resource_id}:
            if key:
                by_key.setdefault(key, []).append(machine)

    items = []
    for name in unique:
        matches = by_key.get(name.casefold(), [])
        if not matches and name.startswith("/"):
            with suppress(Exception):
                matches = by_key.get(normalize_resource_id(name), [])
        items.append({
            "query": name,
            "status": "known" if matches else "unknown",
            "matches": [vm_payload(tree, machine, labels) for machine in matches],
        })

    known = sum(1 for item in items if item["status"] == "known")
    return {"requested": len(unique), "known": known, "unknown": len(unique) - known, "items": items}


@app.post("/api/vms/power-action", status_code=202)
async def vms_power_action(payload: VmPowerActionInput, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    """Start or stop hand-picked machines immediately, without a schedule.

    This is the riskiest path in the product, so a stop must echo the exact machine count and
    protected machines are refused outright rather than silently dropped.
    """
    if not has_permission(user, "schedules.write"):
        raise HTTPException(status_code=403, detail="Permission required: schedules.write")
    machines = list((await db.scalars(select(VirtualMachine).where(VirtualMachine.id.in_(payload.vm_ids)))).all())
    if not machines:
        raise HTTPException(status_code=404, detail="No matching virtual machines")

    tree = await load_tree(db)
    if payload.action == "stop":
        protected = [item for item in machines if is_stop_protected(tree, item)]
        if protected:
            names = ", ".join(sorted(item.vm_name for item in protected)[:5])
            raise HTTPException(status_code=409, detail=f"Protected from stopping: {names}")
        if payload.confirm_count != len(machines):
            raise HTTPException(status_code=422, detail=f"Confirm the stop by sending confirm_count={len(machines)}")

    audit(db, user, f"vm.manual_{payload.action}_requested", "virtual_machine", None, {"vm_count": len(machines), "stop_mode": payload.stop_mode})
    await db.commit()
    run = await trigger_adhoc_run(
        [item.id for item in machines],
        payload.action,
        user.id,
        scheduler,
        stop_mode=payload.stop_mode,
        stagger_seconds=payload.stagger_seconds,
    )
    return run_payload(run, await connection_labels())


@app.post("/api/vms/power-state")
async def vms_power_state(payload: VmPowerScanInput, user: User = Depends(require_permission("vms.read")), db: AsyncSession = Depends(get_db)):
    """Read the live power state of the requested VMs.

    Read-only, so it needs neither the server-wide start flag nor a tenant's `Allow VM starts`.
    One listing call is made per (tenant, subscription) pair rather than one per VM.
    """
    machines = (await db.scalars(select(VirtualMachine).where(VirtualMachine.id.in_(payload.vm_ids)))).all()
    if not machines:
        raise HTTPException(status_code=404, detail="No matching virtual machines")
    tree = await load_tree(db)

    scopes: dict[tuple[str | None, str], list[VirtualMachine]] = {}
    for machine in machines:
        scopes.setdefault((effective_connection_id(tree, machine), machine.subscription_id), []).append(machine)

    states: dict[str, str] = {}
    failures: dict[tuple[str | None, str], str] = {}
    for scope in scopes:
        connection_id, subscription_id = scope
        try:
            connection = await get_connection(connection_id)
            if not connection:
                raise ValueError("No Azure tenant is configured for these virtual machines")
            if connection.get("disabled"):
                raise ValueError("The Azure tenant for these virtual machines is disabled")
            states.update(await read_power_states(connection, subscription_id))
        except Exception as exc:  # noqa: BLE001 - one broken tenant must not fail the whole scan
            failures[scope] = _safe_error(exc)

    labels = await connection_labels()
    items = []
    checked_at = utcnow()
    for machine in machines:
        scope = (effective_connection_id(tree, machine), machine.subscription_id)
        problem = failures.get(scope)
        power = states.get(machine.normalized_resource_id)
        status = "error" if problem else "ok" if power else "not_found"
        if not problem:
            # Cache the reading so the dashboard can summarise the estate without calling ARM.
            machine.last_power_state = power or "not_found"
            machine.last_power_state_at = checked_at
        items.append({
            "vm_id": machine.id,
            "vm_name": machine.vm_name,
            "power_state": None if problem else power,
            "status": status,
            "message": problem or ("" if power else "Not found in Azure under this tenant"),
            **connection_fields(labels, scope[0]),
        })
    await db.commit()
    scanned = sum(1 for item in items if item["status"] == "ok")
    return {
        "checked_at": checked_at,
        "requested": len(payload.vm_ids),
        "scanned": scanned,
        "failed": len(items) - scanned,
        "items": items,
    }


@app.patch("/api/vms/{vm_id}")
async def vm_update(vm_id: str, payload: VmPatch, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "vms.write"):
        raise HTTPException(status_code=403, detail="Permission required: vms.write")
    vm = await load_vm(db, vm_id)
    values = payload.model_dump(exclude_unset=True)
    if values.get("group_id"):
        await load_group(db, values["group_id"])
    for key, value in values.items():
        setattr(vm, key, value)
    audit(db, user, "vm.updated", "virtual_machine", vm.id, values)
    await db.commit()
    tree = await load_tree(db)
    return vm_payload(tree, vm, await connection_labels())


@app.delete("/api/vms/{vm_id}")
async def vm_delete(vm_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "vms.write"):
        raise HTTPException(status_code=403, detail="Permission required: vms.write")
    vm = await load_vm(db, vm_id)
    await db.execute(delete(Schedule).where(Schedule.target_type == "vm", Schedule.target_id == vm.id))
    audit(db, user, "vm.deleted", "virtual_machine", vm.id, {"vm_resource_id": vm.vm_resource_id})
    await db.delete(vm)
    await db.commit()
    return {"ok": True}


@app.post("/api/vms/bulk")
async def vms_bulk(payload: VmBulkAction, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "vms.write"):
        raise HTTPException(status_code=403, detail="Permission required: vms.write")
    vms = (await db.scalars(select(VirtualMachine).where(VirtualMachine.id.in_(payload.vm_ids)))).all()
    if payload.action == "move":
        if not payload.group_id:
            raise HTTPException(status_code=422, detail="group_id is required when moving virtual machines")
        await load_group(db, payload.group_id)
        for vm in vms:
            vm.group_id = payload.group_id
    elif payload.action in {"enable", "disable"}:
        for vm in vms:
            vm.enabled = payload.action == "enable"
    else:
        ids = [vm.id for vm in vms]
        if ids:
            await db.execute(delete(Schedule).where(Schedule.target_type == "vm", Schedule.target_id.in_(ids)))
        for vm in vms:
            await db.delete(vm)
    audit(db, user, f"vm.bulk_{payload.action}", "virtual_machine", payload.group_id, {"count": len(vms)})
    await db.commit()
    return {"ok": True, "affected": len(vms)}


# -- schedules ---------------------------------------------------------


@app.get("/api/schedules")
async def schedules_list(
    q: str | None = None,
    enabled: bool | None = None,
    status: str | None = None,
    action: str | None = None,
    group_id: str | None = None,
    connection_id: str | None = None,
    sort: str | None = None,
    direction: SortDirection = "asc",
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_permission("schedules.read")),
    db: AsyncSession = Depends(get_db),
):
    tree = await load_tree(db)
    labels = await connection_labels()
    statement = select(Schedule)
    if q:
        statement = statement.where(Schedule.name.ilike(f"%{q}%"))
    if enabled is not None:
        statement = statement.where(Schedule.enabled == enabled)
    if status:
        statement = statement.where(Schedule.status == status)
    if action:
        statement = statement.where(Schedule.action == action)
    if connection_id:
        statement = statement.where(Schedule.azure_connection_id == connection_id)
    if group_id:
        scope = tree.subtree_ids(group_id) or {group_id}
        vm_ids = list((await db.scalars(select(VirtualMachine.id).where(VirtualMachine.group_id.in_(list(scope))))).all())
        statement = statement.where(
            ((Schedule.target_type == "group") & Schedule.target_id.in_(list(scope)))
            | ((Schedule.target_type == "vm") & Schedule.target_id.in_(vm_ids or [""]))
        )
    total = await db.scalar(select(func.count()).select_from(statement.subquery()))
    ordered = statement.order_by(Schedule.next_run_at.is_(None), Schedule.next_run_at, Schedule.name) if not sort else _sorted(statement, SCHEDULE_SORT_COLUMNS, sort, direction, "name")
    items = (await db.scalars(ordered.limit(limit).offset(offset))).all()
    vm_names = dict((await db.execute(select(VirtualMachine.id, VirtualMachine.vm_name))).all())
    index = await load_schedule_index(db)
    payloads = []
    for item in items:
        vms = await resolve_schedule_vms(db, item, tree, index)
        payloads.append({**schedule_payload(tree, item, labels, vm_names), "vm_count": len(vms)})
    return {"items": payloads, "total": total or 0, "limit": limit, "offset": offset}


@app.post("/api/schedules/preview")
async def schedule_preview(payload: RecurrencePreviewInput, user: User = Depends(require_permission("schedules.read")), db: AsyncSession = Depends(get_db)):
    """Describe a recurrence and list its next few occurrences, without saving anything.

    The editor calls this on every keystroke, so cron correctness and DST are answered by the same
    engine the scheduler uses rather than being re-implemented in the browser.
    """
    policy = await get_security_policy(db)
    recurrence = Recurrence(
        schedule_type=payload.schedule_type,
        timezone=payload.timezone or resolve_default_timezone(policy),
        start_time=payload.start_time,
        cron_expression=payload.cron_expression,
        weekday=payload.weekday,
        start_date=payload.start_date,
        end_date=payload.end_date,
        run_limit=payload.run_limit,
        run_count=payload.run_count,
    )
    try:
        recurrence_validate(recurrence)
        moments = upcoming_occurrences(recurrence, count=5)
    except RecurrenceError as exc:
        return {"valid": False, "error": str(exc), "description": "", "cron": "", "next_run_at": None, "upcoming": []}
    cron = "" if recurrence.schedule_type == "one_time" else to_cron_expression(recurrence)
    return {
        "valid": True,
        "error": "",
        "description": describe_recurrence(recurrence),
        "cron": cron,
        "next_run_at": moments[0] if moments else None,
        "upcoming": moments,
    }


@app.post("/api/schedules", response_model=ScheduleView, status_code=201)
async def schedule_create(payload: ScheduleInput, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "schedules.write"):
        raise HTTPException(status_code=403, detail="Permission required: schedules.write")
    payload.azure_connection_id = await resolve_schedule_connection(payload.azure_connection_id)
    await assert_target_exists(db, payload.target_type, payload.target_id)
    policy = await get_security_policy(db)
    schedule = Schedule(id=new_id(), name=payload.name, schedule_type=payload.schedule_type, start_time=payload.start_time, timezone=payload.timezone or resolve_default_timezone(policy), target_type=payload.target_type, target_id=payload.target_id, created_by=user.id)
    try:
        apply_schedule_values(schedule, payload, policy.schedule_missed_grace_seconds, resolve_default_timezone(policy))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await reject_duplicate_schedule(db, schedule)
    db.add(schedule)
    audit(db, user, "schedule.created", "schedule", schedule.id, {"name": schedule.name, "target_type": schedule.target_type, "target_id": schedule.target_id})
    await db.commit()
    await db.refresh(schedule)
    return schedule


@app.get("/api/schedules/upcoming")
async def schedules_upcoming(limit: int = Query(10, ge=1, le=100), user: User = Depends(require_permission("schedules.read")), db: AsyncSession = Depends(get_db)):
    tree = await load_tree(db)
    labels = await connection_labels()
    index = await load_schedule_index(db)
    schedules = (await db.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run_at.is_not(None)).order_by(Schedule.next_run_at).limit(limit))).all()
    waves = []
    for schedule in schedules:
        vms = await resolve_schedule_vms(db, schedule, tree, index)
        connection_id = (effective_connection_id(tree, vms[0]) if vms else None) or schedule.azure_connection_id
        waves.append({
            "schedule_id": schedule.id,
            "name": schedule.name,
            "action": schedule.action,
            "stop_mode": schedule.stop_mode,
            "next_run_at": _aware(schedule.next_run_at),
            "timezone": schedule.timezone,
            "stagger_seconds": schedule.stagger_seconds,
            "target_type": schedule.target_type,
            "target_id": schedule.target_id,
            "group_path": tree.name_path(schedule.target_id) if schedule.target_type == "group" else tree.name_path(vms[0].group_id) if vms else "",
            "vm_count": len(vms),
            **connection_fields(labels, connection_id),
        })
    return waves


@app.get("/api/timeline")
async def timeline(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    user: User = Depends(require_permission("schedules.read")),
    db: AsyncSession = Depends(get_db),
):
    window_start = (from_ or utcnow()).astimezone(timezone.utc)
    window_end = (to or window_start + timedelta(days=7)).astimezone(timezone.utc)
    if window_end <= window_start:
        raise HTTPException(status_code=422, detail="to must be later than from")
    if window_end - window_start > timedelta(days=31):
        raise HTTPException(status_code=422, detail="Timeline windows are limited to 31 days")
    tree = await load_tree(db)
    labels = await connection_labels()
    index = await load_schedule_index(db)
    schedules = (await db.scalars(select(Schedule).where(Schedule.enabled.is_(True)))).all()
    blocks: list[dict[str, Any]] = []
    for schedule in schedules:
        vms = await resolve_schedule_vms(db, schedule, tree, index)
        recurrence = recurrence_of(schedule)
        cursor = window_start - timedelta(seconds=1)
        # A dense cron (say every 15 minutes) can fill the window on its own, so cap per schedule.
        for _ in range(200):
            try:
                occurrence = recurrence_next(recurrence, cursor)
            except RecurrenceError:
                break
            if occurrence is None or occurrence >= window_end:
                break
            if occurrence >= window_start:
                blocks.append({
                    "schedule_id": schedule.id,
                    "name": schedule.name,
                    "action": schedule.action,
                    "stop_mode": schedule.stop_mode,
                    "start": occurrence,
                    "end": occurrence + timedelta(seconds=max(schedule.stagger_seconds * max(len(vms) - 1, 0), 60)),
                    "group_path": tree.name_path(schedule.target_id) if schedule.target_type == "group" else tree.name_path(vms[0].group_id) if vms else "",
                    "vm_count": len(vms),
                    "stagger_seconds": schedule.stagger_seconds,
                    **connection_fields(labels, schedule.azure_connection_id),
                })
            cursor = occurrence
            if len(blocks) >= 500:
                break
    return sorted(blocks, key=lambda item: item["start"])[:500]


@app.get("/api/schedules/{schedule_id}")
async def schedule_detail(schedule_id: str, user: User = Depends(require_permission("schedules.read")), db: AsyncSession = Depends(get_db)):
    schedule = await db.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    tree = await load_tree(db)
    labels = await connection_labels()
    vm_names = dict((await db.execute(select(VirtualMachine.id, VirtualMachine.vm_name))).all())
    attempts = (await db.scalars(select(VmAttempt).where(VmAttempt.schedule_id == schedule_id).order_by(VmAttempt.claimed_at.desc()).limit(200))).all()
    runs = (await db.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == schedule_id).order_by(ScheduleRun.created_at.desc()).limit(20))).all()
    vms = await resolve_schedule_vms(db, schedule, tree)
    return {
        "schedule": schedule_payload(tree, schedule, labels, vm_names),
        "vms": [vm_payload(tree, item, labels) for item in vms],
        "attempts": [attempt_payload(item, labels) for item in attempts],
        "runs": [run_payload(item, labels) for item in runs],
    }


@app.patch("/api/schedules/{schedule_id}", response_model=ScheduleView)
async def schedule_update(schedule_id: str, payload: SchedulePatch, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "schedules.write"):
        raise HTTPException(status_code=403, detail="Permission required: schedules.write")
    schedule = await db.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if "azure_connection_id" in payload.model_fields_set:
        payload.azure_connection_id = await resolve_schedule_connection(payload.azure_connection_id)
    elif not schedule.azure_connection_id:
        payload.azure_connection_id = await resolve_schedule_connection(None)
    if payload.target_type or payload.target_id:
        await assert_target_exists(db, payload.target_type or schedule.target_type, payload.target_id or schedule.target_id)
    policy = await get_security_policy(db)
    try:
        apply_schedule_values(schedule, payload, policy.schedule_missed_grace_seconds, resolve_default_timezone(policy))
        await reject_duplicate_schedule(db, schedule)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(db, user, "schedule.updated", "schedule", schedule.id, payload.model_dump(exclude_unset=True))
    await db.commit()
    await db.refresh(schedule)
    return schedule


@app.delete("/api/schedules/{schedule_id}")
async def schedule_delete(schedule_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "schedules.write"):
        raise HTTPException(status_code=403, detail="Permission required: schedules.write")
    schedule = await db.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    audit(db, user, "schedule.deleted", "schedule", schedule.id, {"name": schedule.name})
    await db.delete(schedule)
    await db.commit()
    return {"ok": True}


@app.get("/api/schedules/{schedule_id}/attempts")
async def schedule_attempts(schedule_id: str, limit: int = Query(200, ge=1, le=1000), user: User = Depends(require_permission("runs.read")), db: AsyncSession = Depends(get_db)):
    if not await db.get(Schedule, schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    labels = await connection_labels()
    attempts = (await db.scalars(select(VmAttempt).where(VmAttempt.schedule_id == schedule_id).order_by(VmAttempt.claimed_at.desc()).limit(limit))).all()
    return [attempt_payload(item, labels) for item in attempts]


@app.post("/api/schedules/{schedule_id}/run")
async def schedule_run(schedule_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "schedules.write"):
        raise HTTPException(status_code=403, detail="Permission required: schedules.write")
    schedule = await db.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await resolve_schedule_connection(schedule.azure_connection_id)
    audit(db, user, "schedule.run_requested", "schedule", schedule.id, {"name": schedule.name})
    await db.commit()
    run = await trigger_schedule_run(schedule_id, user.id, scheduler)
    return run_payload(run, await connection_labels()) if run else None


# -- runs --------------------------------------------------------------


@app.get("/api/runs")
async def runs_list(
    schedule_id: str | None = None,
    status: str | None = None,
    trigger: str | None = None,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    sort: str | None = None,
    direction: SortDirection = "desc",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_permission("runs.read")),
    db: AsyncSession = Depends(get_db),
):
    labels = await connection_labels()
    statement = select(ScheduleRun)
    if schedule_id:
        statement = statement.where(ScheduleRun.schedule_id == schedule_id)
    if status:
        statement = statement.where(ScheduleRun.status == status)
    if trigger:
        statement = statement.where(ScheduleRun.trigger == trigger)
    if from_:
        statement = statement.where(ScheduleRun.created_at >= from_.astimezone(timezone.utc))
    if to:
        statement = statement.where(ScheduleRun.created_at <= to.astimezone(timezone.utc))
    total = await db.scalar(select(func.count()).select_from(statement.subquery()))
    ordered = _sorted(statement, RUN_SORT_COLUMNS, sort, direction, "created_at")
    items = (await db.scalars(ordered.limit(limit).offset(offset))).all()
    return {"items": [run_payload(item, labels) for item in items], "total": total or 0, "limit": limit, "offset": offset}


ATTEMPT_SEVERITY = {"failed": "error", "timed_out": "error", "cancelled": "warning", "skipped": "warning", "succeeded": "success"}
RUN_SEVERITY = {"failed": "error", "timed_out": "error", "partially_failed": "warning", "skipped": "warning", "succeeded": "success"}


@app.get("/api/runs/activity")
async def runs_activity(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(500, ge=1, le=2000),
    user: User = Depends(require_permission("runs.read")),
    db: AsyncSession = Depends(get_db),
):
    """Flat, newest-first log of wave and per-VM events, for the run timeline."""
    window_end = (to or utcnow()).astimezone(timezone.utc)
    window_start = (from_ or window_end - timedelta(days=1)).astimezone(timezone.utc)
    if window_end <= window_start:
        raise HTTPException(status_code=422, detail="to must be later than from")
    labels = await connection_labels()

    runs = (await db.scalars(
        select(ScheduleRun)
        .where(ScheduleRun.created_at >= window_start, ScheduleRun.created_at <= window_end)
        .order_by(ScheduleRun.created_at.desc())
        .limit(limit)
    )).all()
    run_by_id = {run.id: run for run in runs}
    attempts: list[VmAttempt] = []
    if run_by_id:
        attempts = list((await db.scalars(
            select(VmAttempt)
            .where(VmAttempt.run_id.in_(list(run_by_id)))
            .order_by(VmAttempt.sequence)
            .limit(limit * 4)
        )).all())

    events: list[dict[str, Any]] = []
    for run in runs:
        events.append({
            "id": f"run:{run.id}:started",
            "at": _aware(run.started_at or run.created_at),
            "kind": "Wave",
            "severity": "info",
            "title": run.schedule_name,
            "summary": f"Started a {run.trigger} wave covering {run.total_count} virtual machine{'' if run.total_count == 1 else 's'}.",
            "run_id": run.id,
            "status": run.status,
            "mode": run.mode,
        })
        if run.finished_at:
            events.append({
                "id": f"run:{run.id}:finished",
                "at": _aware(run.finished_at),
                "kind": "Wave",
                "severity": RUN_SEVERITY.get(run.status, "info"),
                "title": run.schedule_name,
                "summary": f"Wave {run.status.replace('_', ' ')} — {run.succeeded_count} started, {run.failed_count} failed, {run.skipped_count} skipped.",
                "run_id": run.id,
                "status": run.status,
                "mode": run.mode,
            })
    for attempt in attempts:
        run = run_by_id.get(attempt.run_id or "")
        stamp = _aware(attempt.completed_at or attempt.started_at or attempt.claimed_at)
        if not stamp:
            continue
        name = attempt.vm_resource_id.rsplit("/", 1)[-1] or "virtual machine"
        events.append({
            "id": f"attempt:{attempt.id}",
            "at": stamp,
            "kind": "Start attempt",
            "severity": ATTEMPT_SEVERITY.get(attempt.status, "info"),
            "title": name,
            "summary": attempt.message or f"Start attempt {attempt.status.replace('_', ' ')}.",
            "run_id": attempt.run_id,
            "attempt_id": attempt.id,
            "schedule_name": run.schedule_name if run else "",
            "status": attempt.status,
            "mode": attempt.mode,
            **connection_fields(labels, attempt.connection_id),
        })

    events.sort(key=lambda item: (item["at"], item["id"]), reverse=True)
    return {
        "from": window_start,
        "to": window_end,
        "events": events[:limit],
        "truncated": len(events) > limit,
    }


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str, user: User = Depends(require_permission("runs.read")), db: AsyncSession = Depends(get_db)):
    run = await db.get(ScheduleRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    labels = await connection_labels()
    attempts = (await db.scalars(select(VmAttempt).where(VmAttempt.run_id == run_id).order_by(VmAttempt.sequence))).all()
    return {"run": run_payload(run, labels), "attempts": [attempt_payload(item, labels) for item in attempts]}


@app.post("/api/runs/{run_id}/retry-failed")
async def run_retry_failed(run_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "schedules.write"):
        raise HTTPException(status_code=403, detail="Permission required: schedules.write")
    run = await db.get(ScheduleRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    vm_ids = list((await db.scalars(select(VmAttempt.vm_id).where(VmAttempt.run_id == run_id, VmAttempt.status.in_(["failed", "timed_out"])))).all())
    vm_ids = [item for item in vm_ids if item]
    if not vm_ids:
        raise HTTPException(status_code=409, detail="This run has no failed virtual machines to retry")
    audit(db, user, "run.retry_requested", "schedule_run", run_id, {"vm_count": len(vm_ids), "action": run.action})
    await db.commit()
    # An on-demand run has no schedule to re-trigger, so retry it as another on-demand wave.
    if run.schedule_id:
        retry = await trigger_schedule_run(run.schedule_id, user.id, scheduler, vm_ids=vm_ids)
    else:
        retry = await trigger_adhoc_run(vm_ids, run.action, user.id, scheduler, stop_mode=run.stop_mode)
    return run_payload(retry, await connection_labels()) if retry else None


@app.post("/api/attempts/{attempt_id}/retry")
async def attempt_retry(attempt_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "schedules.write"):
        raise HTTPException(status_code=403, detail="Permission required: schedules.write")
    previous = await db.get(VmAttempt, attempt_id)
    if not previous:
        raise HTTPException(status_code=404, detail="Attempt not found")
    audit(db, user, "attempt.retry_requested", "vm_attempt", attempt_id, {"vm_id": previous.vm_id, "action": previous.action})
    await db.commit()
    if previous.schedule_id:
        run = await trigger_schedule_run(previous.schedule_id, user.id, scheduler, vm_ids=[previous.vm_id] if previous.vm_id else None)
    elif previous.vm_id:
        run = await trigger_adhoc_run([previous.vm_id], previous.action, user.id, scheduler, stop_mode=previous.stop_mode)
    else:
        raise HTTPException(status_code=409, detail="This attempt is no longer linked to a virtual machine")
    return run_payload(run, await connection_labels()) if run else None


@app.post("/api/imports/preview")
async def csv_preview(
    file: UploadFile = File(...),
    connection_id: str | None = Form(None),
    default_group_id: str | None = Form(None),
    user: User = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
):
    if not has_permission(user, "imports.write"):
        raise HTTPException(status_code=403, detail="Permission required: imports.write")
    content = await file.read()
    if len(content) > 2_000_000:
        raise HTTPException(status_code=413, detail="CSV exceeds 2 MB")
    default_path: list[str] | None = None
    if default_group_id:
        group = await load_group(db, default_group_id)
        tree = await load_tree(db)
        # chain() is nearest-first, so reverse it to get root -> leaf segments.
        default_path = [node.name for node in reversed(tree.chain(group.id))] or [group.name]
    try:
        policy = await get_security_policy(db)
        result = await validate_csv(content, policy.schedule_missed_grace_seconds, db, connection_id or None, default_path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = [item["data"] for item in result["rows"]]
    return {"filename": file.filename or "import.csv", "preview_token": _preview_token(rows), **result}


async def _ensure_group_path(db: AsyncSession, segments: list[str], user: User, created: list[str]) -> Group:
    return await ensure_group_path(db, segments, created_by=user.id, created=created)


async def _commit_inventory(payload: CsvCommitRequest, user: User, db: AsyncSession) -> dict[str, Any]:
    created_groups: list[str] = []
    created_vms: list[VirtualMachine] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(payload.rows, start=1):
        try:
            resource_id = str(row.get("vm_resource_id", ""))
            parsed = parse_vm_resource_id(resource_id)
            normalized = normalize_resource_id(resource_id)
            if normalized in seen or await db.scalar(select(VirtualMachine.id).where(VirtualMachine.normalized_resource_id == normalized)):
                raise ValueError("This VM is already in the inventory")
            seen.add(normalized)
            segments = [str(row.get("application", ""))] + [item for item in str(row.get("ring_path", "")).split("/") if item.strip()]
            group = await _ensure_group_path(db, [item.strip() for item in segments if item.strip()], user, created_groups)
            created_vms.append(VirtualMachine(id=new_id(), group_id=group.id, vm_resource_id=resource_id.strip(), normalized_resource_id=normalized, display_name=str(row.get("display_name") or parsed.vm_name), subscription_id=parsed.subscription_id, resource_group=parsed.resource_group, vm_name=parsed.vm_name, azure_connection_id=row.get("azure_connection_id"), enabled=bool(row.get("enabled", True)), never_stop=bool(row.get("never_stop", False)), notes=str(row.get("notes", "")), created_by=user.id))
        except Exception as exc:
            errors.append({"row": index, "error": str(exc)})
    if errors and payload.reject_all:
        await db.rollback()
        raise HTTPException(status_code=422, detail={"message": "Import rejected atomically; nothing was created", "errors": errors})
    db.add_all(created_vms)
    batch = ImportBatch(id=new_id(), filename=payload.filename, row_count=len(payload.rows), accepted_count=len(created_vms), rejected_count=len(errors), created_by=user.id)
    db.add(batch)
    audit(db, user, "import.committed", "import_batch", batch.id, {"format": "inventory", "groups": len(created_groups), "vms": len(created_vms), "rejected": len(errors)})
    await db.commit()
    return {"batch_id": batch.id, "format": "inventory", "accepted": len(created_vms), "groups_created": len(created_groups), "rejected": len(errors), "errors": errors}


@app.post("/api/imports/commit")
async def csv_commit(payload: CsvCommitRequest, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "imports.write"):
        raise HTTPException(status_code=403, detail="Permission required: imports.write")
    _validate_preview_token(payload.preview_token, payload.rows)
    if any("application" in row for row in payload.rows):
        return await _commit_inventory(payload, user, db)
    policy = await get_security_policy(db)
    default_zone = resolve_default_timezone(policy)
    pending: list[Schedule] = []
    errors: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    created_groups: list[str] = []
    for index, row in enumerate(payload.rows, start=1):
        try:
            resource_id = str(row.get("vm_resource_id", ""))
            parsed = parse_vm_resource_id(resource_id)
            normalized = normalize_resource_id(resource_id)
            vm = await db.scalar(select(VirtualMachine).where(VirtualMachine.normalized_resource_id == normalized))
            if not vm:
                group = await _ensure_group_path(db, ["Ungrouped"], user, created_groups)
                vm = VirtualMachine(id=new_id(), group_id=group.id, vm_resource_id=resource_id.strip(), normalized_resource_id=normalized, display_name=parsed.vm_name, subscription_id=parsed.subscription_id, resource_group=parsed.resource_group, vm_name=parsed.vm_name, created_by=user.id)
                db.add(vm)
                await db.flush()
            parsed_row = ScheduleInput.model_validate({**row, "target_type": "vm", "target_id": vm.id})
            parsed_row.azure_connection_id = await resolve_schedule_connection(parsed_row.azure_connection_id)
            schedule = Schedule(id=new_id(), name=parsed_row.name, schedule_type=parsed_row.schedule_type, start_time=parsed_row.start_time, timezone=parsed_row.timezone or default_zone, target_type="vm", target_id=vm.id, created_by=user.id)
            apply_schedule_values(schedule, parsed_row, policy.schedule_missed_grace_seconds, default_zone)
            key = (schedule.action, schedule.schedule_type, schedule.start_time, schedule.target_id)
            if key in seen:
                raise ValueError("Duplicate schedule in import")
            seen.add(key)
            await reject_duplicate_schedule(db, schedule)
            pending.append(schedule)
        except HTTPException as exc:
            errors.append({"row": index, "error": str(exc.detail)})
        except Exception as exc:
            errors.append({"row": index, "error": str(exc)})
    if errors and payload.reject_all:
        await db.rollback()
        raise HTTPException(status_code=422, detail={"message": "Import rejected atomically; no schedules were created", "errors": errors})
    db.add_all(pending)
    batch = ImportBatch(id=new_id(), filename=payload.filename, row_count=len(payload.rows), accepted_count=len(pending), rejected_count=len(errors), created_by=user.id)
    db.add(batch)
    audit(db, user, "import.committed", "import_batch", batch.id, {"format": "schedules", "accepted": len(pending), "rejected": len(errors)})
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return {"batch_id": batch.id, "format": "schedules", "accepted": len(pending), "rejected": len(errors), "errors": errors}


@app.get("/api/connections")
async def connections_list(user: User = Depends(require_permission("schedules.read"))):
    return await list_connections(public=True)


@app.put("/api/connections")
async def connection_upsert(payload: ConnectionInput, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator permission required")
    try:
        result = await upsert_connection(payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(db, user, "connection.upserted", "connection", result["id"], {"auth_method": result["auth_method"]})
    await db.commit()
    return result


@app.delete("/api/connections/{connection_id}")
async def connection_delete(connection_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator permission required")
    if not await delete_connection(connection_id):
        raise HTTPException(status_code=404, detail="Connection not found")
    audit(db, user, "connection.deleted", "connection", connection_id)
    await db.commit()
    return {"ok": True}


@app.post("/api/connections/{connection_id}/default")
async def connection_default(connection_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator permission required")
    try:
        result = await set_default(connection_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Connection not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit(db, user, "connection.defaulted", "connection", connection_id)
    await db.commit()
    return result


async def connection_live_action(connection_id: str, action: str, user: User, db: AsyncSession):
    connection = await get_connection(connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")
    if connection.get("disabled"):
        raise HTTPException(status_code=409, detail="Connection is disabled")
    try:
        subscriptions = await discover(connection)
        await update_connection_status(connection_id, True, f"Connected; {len(subscriptions)} subscription(s)")
        audit(db, user, f"connection.{action}", "connection", connection_id, {"ok": True, "subscriptions": len(subscriptions)})
        await db.commit()
        return {"ok": True, "subscriptions": subscriptions}
    except Exception as exc:
        safe_message = _safe_error(exc)
        await update_connection_status(connection_id, False, safe_message)
        audit(db, user, f"connection.{action}", "connection", connection_id, {"ok": False})
        await db.commit()
        await publish(
            db,
            type="connection.unhealthy",
            severity="error",
            title=f"Azure connection {connection.get('display_name') or connection_id} is unhealthy",
            body=f"The {action} probe failed: {safe_message}",
            facts={"tenant": connection.get("display_name") or connection.get("tenant_id") or "", "error": safe_message},
            connection_id=connection_id,
        )
        raise HTTPException(status_code=502, detail=f"Azure connection failed: {safe_message}") from exc


@app.post("/api/connections/{connection_id}/test")
async def connection_test(connection_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator permission required")
    return await connection_live_action(connection_id, "tested", user, db)


@app.get("/api/connections/{connection_id}/discover")
async def connection_discover(connection_id: str, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await connection_live_action(connection_id, "discovered", user, db)


@app.get("/api/connections/{connection_id}/vms")
async def connection_vms(connection_id: str, subscription_id: str = Query(...), user: User = Depends(require_permission("vms.read")), db: AsyncSession = Depends(get_db)):
    """Live ARM lookup; never triggered by scheduling."""
    try:
        subscription = parse_subscription_id(subscription_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    connection = await get_connection(connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")
    if connection.get("disabled"):
        raise HTTPException(status_code=409, detail="Connection is disabled")
    try:
        machines = await list_virtual_machines(connection, subscription)
    except Exception as exc:
        safe_message = _safe_error(exc)
        audit(db, user, "connection.vms_listed", "connection", connection_id, {"ok": False, "subscription_id": subscription})
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Azure virtual machine lookup failed: {safe_message}") from exc
    known = set((await db.scalars(select(VirtualMachine.normalized_resource_id))).all())
    audit(db, user, "connection.vms_listed", "connection", connection_id, {"ok": True, "subscription_id": subscription, "count": len(machines)})
    await db.commit()
    return {"subscription_id": subscription, "count": len(machines), "items": [{**item, "already_imported": normalize_resource_id(item["id"]) in known} for item in machines]}


@app.post("/api/connections/{connection_id}/resolve-vms")
async def connection_resolve_vms(connection_id: str, payload: VmNameResolveInput, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    """Resolve bare VM names to full resource IDs across the tenant. Live ARM lookup; never triggered by scheduling."""
    if not has_permission(user, "vms.read"):
        raise HTTPException(status_code=403, detail="Permission required: vms.read")
    names = [item.strip() for item in payload.names if item.strip()]
    if not names:
        raise HTTPException(status_code=422, detail="Provide at least one virtual machine name")
    unique = list(dict.fromkeys(names))
    if len(unique) > 500:
        raise HTTPException(status_code=422, detail="Resolve at most 500 names at a time")
    connection = await get_connection(connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")
    if connection.get("disabled"):
        raise HTTPException(status_code=409, detail="Connection is disabled")
    try:
        subscriptions = [parse_subscription_id(item) for item in payload.subscription_ids] if payload.subscription_ids else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        candidates, source = await resolve_vm_names(connection, unique, subscriptions)
    except Exception as exc:
        safe_message = _safe_error(exc)
        audit(db, user, "connection.vms_resolved", "connection", connection_id, {"ok": False, "requested": len(unique)})
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Azure name resolution failed: {safe_message}") from exc

    subscription_names: dict[str, str] = {}
    with suppress(Exception):
        subscription_names = {item["id"]: item["name"] for item in await discover(connection)}

    known = {row[0]: row[1] for row in (await db.execute(select(VirtualMachine.normalized_resource_id, VirtualMachine.group_id))).all()}
    tree = await load_tree(db)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        normalized = normalize_resource_id(item["id"])
        existing_group = known.get(normalized)
        by_name.setdefault(item["name"].casefold(), []).append({
            "vm_resource_id": item["id"],
            "name": item["name"],
            "resource_group": item["resource_group"],
            "subscription_id": item["subscription_id"],
            "subscription_name": subscription_names.get(item["subscription_id"]),
            "location": item.get("location", ""),
            "already_imported": existing_group is not None,
            "group_path": tree.name_path(existing_group) if existing_group else None,
        })

    items = []
    for name in unique:
        matches = sorted(by_name.get(name.casefold(), []), key=lambda entry: (entry["subscription_id"], entry["resource_group"]))
        status = "not_found" if not matches else "ambiguous" if len(matches) > 1 else "resolved"
        items.append({"query": name, "status": status, "matches": matches})

    resolved = sum(1 for item in items if item["status"] == "resolved")
    audit(db, user, "connection.vms_resolved", "connection", connection_id, {"ok": True, "requested": len(unique), "resolved": resolved, "source": source})
    await db.commit()
    return {
        "source": source,
        "requested": len(unique),
        "resolved": resolved,
        "ambiguous": sum(1 for item in items if item["status"] == "ambiguous"),
        "not_found": sum(1 for item in items if item["status"] == "not_found"),
        "items": items,
    }


@app.get("/api/audit", response_model=list[AuditView])
async def audit_list(limit: int = Query(100, ge=1, le=500), action: str | None = None, user: User = Depends(require_permission("audit.read")), db: AsyncSession = Depends(get_db)):
    statement = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        statement = statement.where(AuditLog.action.ilike(f"%{action}%"))
    return (await db.scalars(statement)).all()


# -- connectors --------------------------------------------------------


def require_manage(user: User, permission: str) -> None:
    if not has_permission(user, permission):
        raise HTTPException(status_code=403, detail=f"Permission required: {permission}")


async def load_connector(connector_id: str) -> dict[str, Any]:
    connector = await get_connector(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    return connector


@app.get("/api/connectors")
async def connectors_list(user: User = Depends(require_permission("connectors.read"))):
    return {"connectors": await list_connectors(public=True), "types": type_metadata(), "event_types": list(EVENT_TYPES)}


@app.put("/api/connectors")
async def connector_upsert(payload: ConnectorInput, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    require_manage(user, "connectors.manage")
    try:
        result = await upsert_connector(payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(db, user, "connector.upserted", "connector", result["id"], {"type": result["type"], "mode": result["mode"]})
    await db.commit()
    return result


@app.delete("/api/connectors/{connector_id}")
async def connector_delete(connector_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    require_manage(user, "connectors.manage")
    if not await delete_connector(connector_id):
        raise HTTPException(status_code=404, detail="Connector not found")
    audit(db, user, "connector.deleted", "connector", connector_id)
    await db.commit()
    return {"ok": True}


@app.post("/api/connectors/{connector_id}/test")
async def connector_test(connector_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    """Authentication probe only; no message is ever sent and no ticket is ever opened."""
    require_manage(user, "connectors.manage")
    connector = await load_connector(connector_id)
    try:
        result = await test_connector(connector)
    except Exception as exc:
        detail = sanitize_detail(exc)
        await update_connector_status(connector_id, False, detail)
        audit(db, user, "connector.tested", "connector", connector_id, {"ok": False})
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Connector test failed: {detail}") from exc
    detail = str(result.get("detail") or "Connector reachable")
    await update_connector_status(connector_id, True, detail)
    audit(db, user, "connector.tested", "connector", connector_id, {"ok": True})
    await db.commit()
    return {"ok": True, "detail": detail, "connector": await get_connector_public(connector_id)}


@app.post("/api/connectors/{connector_id}/send-test")
async def connector_send_test(connector_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    require_manage(user, "connectors.manage")
    connector = await load_connector(connector_id)
    if connector["type"] == "servicenow":
        raise HTTPException(status_code=409, detail="Send test is blocked for ServiceNow so no real incident is created; use Test instead")
    message = build_message(
        "connector.test",
        "info",
        "Azure VM Scheduler test notification",
        "This is a test message from Azure VM Scheduler. No virtual machines were started.",
        {"application": "Sample application", "ring": "Ring 1", "schedule_name": "Sample schedule", "vm_count": 3, "succeeded": 3, "failed": 0, "tenant": "Sample tenant", "run_url": get_settings().base_url},
    )
    try:
        result = await send_via_connector(connector, message)
    except Exception as exc:
        detail = sanitize_detail(exc)
        await update_connector_status(connector_id, False, detail)
        audit(db, user, "connector.send_test", "connector", connector_id, {"ok": False})
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Test message failed: {detail}") from exc
    detail = str(result.get("detail") or "Test message sent")
    await update_connector_status(connector_id, True, detail)
    audit(db, user, "connector.send_test", "connector", connector_id, {"ok": True})
    await db.commit()
    return {"ok": True, "detail": detail, "connector": await get_connector_public(connector_id)}


async def get_connector_public(connector_id: str) -> dict[str, Any] | None:
    return next((item for item in await list_connectors(public=True) if item["id"] == connector_id), None)


# -- notification rules ------------------------------------------------


def rule_payload(rule: NotificationRule) -> dict[str, Any]:
    return NotificationRuleView.model_validate(rule).model_dump(mode="json")


@app.get("/api/notification-rules")
async def notification_rules_list(user: User = Depends(require_permission("notifications.read")), db: AsyncSession = Depends(get_db)):
    rules = (await db.scalars(select(NotificationRule).order_by(NotificationRule.created_at))).all()
    return {"rules": [rule_payload(item) for item in rules], "event_types": list(EVENT_TYPES), "connectors": await list_connectors(public=True)}


@app.put("/api/notification-rules")
async def notification_rule_upsert(payload: NotificationRuleInput, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    require_manage(user, "notifications.manage")
    known = {item["id"] for item in await list_connectors(public=True)}
    unknown = [item for item in payload.connector_ids if item not in known]
    if unknown:
        raise HTTPException(status_code=422, detail="One or more selected connectors do not exist")
    if payload.scope_group_id and not await db.get(Group, payload.scope_group_id):
        raise HTTPException(status_code=422, detail="scope_group_id does not exist")
    values = payload.model_dump(exclude={"id"})
    rule = await db.get(NotificationRule, payload.id) if payload.id else None
    if payload.id and not rule:
        raise HTTPException(status_code=404, detail="Notification rule not found")
    if not rule:
        rule = NotificationRule(id=new_id(), created_by=user.id)
        db.add(rule)
    for key, value in values.items():
        setattr(rule, key, value)
    await db.flush()
    audit(db, user, "notification_rule.upserted", "notification_rule", rule.id, {"enabled": rule.enabled, "digest_mode": rule.digest_mode, "connectors": len(rule.connector_ids)})
    await db.commit()
    return rule_payload(rule)


@app.delete("/api/notification-rules/{rule_id}")
async def notification_rule_delete(rule_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    require_manage(user, "notifications.manage")
    rule = await db.get(NotificationRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Notification rule not found")
    await db.delete(rule)
    audit(db, user, "notification_rule.deleted", "notification_rule", rule_id)
    await db.commit()
    return {"ok": True}


# -- notification feed -------------------------------------------------


@app.get("/api/notifications")
async def notifications_list(
    unread_only: bool = False,
    severity: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_permission("notifications.read")),
    db: AsyncSession = Depends(get_db),
):
    statement = select(NotificationEvent)
    if unread_only:
        statement = statement.where(NotificationEvent.read.is_(False))
    if severity:
        statement = statement.where(NotificationEvent.severity == severity)
    total = await db.scalar(select(func.count()).select_from(statement.subquery()))
    items = (await db.scalars(statement.order_by(NotificationEvent.created_at.desc()).limit(limit).offset(offset))).all()
    return {"items": [NotificationEventView.model_validate(item).model_dump(mode="json") for item in items], "total": total or 0, "limit": limit, "offset": offset, "unread": await unread_count(db)}


@app.get("/api/notifications/unread-count")
async def notifications_unread_count(user: User = Depends(require_permission("notifications.read")), db: AsyncSession = Depends(get_db)):
    return {"count": await unread_count(db)}


@app.post("/api/notifications/read-all")
async def notifications_read_all(user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    require_manage(user, "notifications.read")
    result = await db.execute(update(NotificationEvent).where(NotificationEvent.read.is_(False)).values(read=True))
    audit(db, user, "notification.read_all", "notification_event", None, {"count": result.rowcount})
    await db.commit()
    return {"ok": True, "count": result.rowcount}


@app.post("/api/notifications/{event_id}/read")
async def notification_read(event_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    require_manage(user, "notifications.read")
    event = await db.get(NotificationEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Notification not found")
    event.read = True
    audit(db, user, "notification.read", "notification_event", event_id)
    await db.commit()
    return {"ok": True}


# -- deliveries --------------------------------------------------------


@app.get("/api/deliveries")
async def deliveries_list(
    status: str | None = None,
    connector_id: str | None = None,
    event_id: str | None = None,
    sort: str | None = None,
    direction: SortDirection = "desc",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_permission("notifications.read")),
    db: AsyncSession = Depends(get_db),
):
    statement = select(NotificationDelivery)
    if status:
        statement = statement.where(NotificationDelivery.status == status)
    if connector_id:
        statement = statement.where(NotificationDelivery.connector_id == connector_id)
    if event_id:
        statement = statement.where(NotificationDelivery.event_id == event_id)
    total = await db.scalar(select(func.count()).select_from(statement.subquery()))
    ordered = _sorted(statement, DELIVERY_SORT_COLUMNS, sort, direction, "created_at")
    items = (await db.scalars(ordered.limit(limit).offset(offset))).all()
    return {"items": [NotificationDeliveryView.model_validate(item).model_dump(mode="json") for item in items], "total": total or 0, "limit": limit, "offset": offset}


@app.post("/api/deliveries/{delivery_id}/retry")
async def delivery_retry(delivery_id: str, user: User = Depends(require_csrf), db: AsyncSession = Depends(get_db)):
    require_manage(user, "notifications.manage")
    delivery = await db.get(NotificationDelivery, delivery_id)
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")
    if delivery.status == "pending":
        raise HTTPException(status_code=409, detail="This delivery is already queued")
    delivery.status = "pending"
    delivery.next_attempt_at = utcnow()
    delivery.detail = "Requeued manually"
    audit(db, user, "delivery.retry_requested", "notification_delivery", delivery_id, {"connector_id": delivery.connector_id})
    await db.commit()
    delivery_service.submit(delivery_id)
    return NotificationDeliveryView.model_validate(delivery).model_dump(mode="json")


# -- single-page app ---------------------------------------------------
# In the container image the built React bundle is copied to app/static and served from here, so
# API and UI share one origin and one port. Mounted last so it can never shadow an /api route.
# When the directory is absent (local dev, where Vite serves the UI) nothing is registered.

STATIC_DIR = Path(__file__).resolve().parent / "static"

if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        """Serve index.html for client-side routes; real files still win."""
        if full_path.startswith(("api/", "healthz", "readyz", "health")):
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (STATIC_DIR / full_path).resolve()
        # Containment check: a crafted path must not escape the static directory.
        if full_path and candidate.is_file() and candidate.is_relative_to(STATIC_DIR):
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
