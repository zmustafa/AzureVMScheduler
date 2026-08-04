from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

from .validation import validate_timezone


def _as_utc_iso(value: datetime | None) -> str | None:
    """Stored timestamps are UTC but tz-naive; always emit an explicit offset so clients cannot misread them as local time."""
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()


UtcDatetime = Annotated[datetime, PlainSerializer(_as_utc_iso, return_type=str | None, when_used="json")]


#: Historical fixed role names. Roles are database rows now, so these are only the seeded ones and
#: are no longer an allow-list — a role name is checked against the roles table by the API instead.
ROLES = {"admin", "operator", "auditor", "viewer", "noaccess"}


def validate_role(value: str) -> str:
    """Shape check only. Existence is verified against the roles table where the role is used."""
    if not value or not value.strip():
        raise ValueError("role must not be empty")
    if len(value) > 64:
        raise ValueError("role must be 64 characters or fewer")
    return value


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class IdentityProviderUpdate(BaseModel):
    enabled: bool
    tenant_id: str = Field(default="", max_length=100)
    client_id: str = Field(default="", max_length=100)
    client_secret: str | None = Field(default=None, max_length=4000)
    auto_provision: bool = False
    default_role: str = "viewer"

    _role = field_validator("default_role")(validate_role)


class SecurityPolicyUpdate(BaseModel):
    local_login_enabled: bool = True
    password_min_length: int = Field(default=12, ge=10, le=128)
    password_require_upper: bool = True
    password_require_lower: bool = True
    password_require_number: bool = True
    password_require_symbol: bool = True
    lockout_attempts: int = Field(default=5, ge=1, le=50)
    lockout_minutes: int = Field(default=15, ge=1, le=1440)
    ip_lockout_enabled: bool = True
    ip_lockout_attempts: int = Field(default=15, ge=1, le=1000)
    ip_lockout_window_seconds: int = Field(default=300, ge=10, le=86400)
    ip_lockout_seconds: int = Field(default=900, ge=10, le=86400)
    allow_self_registration: bool = False
    session_idle_minutes: int = Field(default=60, ge=1, le=10080)
    session_absolute_hours: int = Field(default=12, ge=1, le=720)
    schedule_missed_grace_seconds: int = Field(default=300, ge=0, le=86400)
    default_timezone: str = Field(default="America/New_York", max_length=100)

    @field_validator("default_timezone")
    @classmethod
    def timezone_is_valid(cls, value: str) -> str:
        return validate_timezone(value)


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.@-]+$")
    email: str | None = Field(default=None, max_length=320)
    password: str = Field(min_length=1, max_length=1000)
    role: str = "viewer"
    role_ids: list[str] | None = None
    access_group_ids: list[str] = Field(default_factory=list)

    _role = field_validator("role")(validate_role)


class UserUpdate(BaseModel):
    role: str | None = None
    role_ids: list[str] | None = None
    access_group_ids: list[str] | None = None
    email: str | None = Field(default=None, max_length=320)
    disabled: bool | None = None

    @field_validator("role")
    @classmethod
    def role_is_valid(cls, value: str | None) -> str | None:
        return validate_role(value) if value is not None else value


class RoleInput(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)
    permissions: list[str] = Field(default_factory=list)


class AccessGroupInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    role_ids: list[str] = Field(default_factory=list)


class IdentityProviderInput(BaseModel):
    """A sign-in provider. `client_secret` is write-only; blank keeps the stored one.

    `entra` is OIDC with the issuer derived from a directory id; `oidc` is any other compliant
    issuer; `saml` is a SAML 2.0 identity provider.
    """

    id: str | None = None
    name: str = Field(min_length=1, max_length=128)
    type: Literal["entra", "oidc", "saml"] = "entra"
    enabled: bool = False
    button_label: str = Field(default="", max_length=128)
    config: dict[str, Any] = Field(default_factory=dict)
    client_secret: str | None = Field(default=None, max_length=4000)


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=1, max_length=1000)


class IpRuleInput(BaseModel):
    """An allowed address or range.

    `allow_any` exists so that "let the whole internet in" cannot be typed by accident: a rule of
    ``0.0.0.0/0`` silently defeats the feature, so it has to be asked for twice.
    """

    cidr: str = Field(min_length=1, max_length=64)
    label: str = Field(default="", max_length=200)
    enabled: bool = True
    allow_any: bool = False


class IpRulePatch(BaseModel):
    cidr: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    allow_any: bool = False


class IpPolicyUpdate(BaseModel):
    mode: Literal["disabled", "audit", "enforce"]
    scope: Literal["auth_only", "all"] = "auth_only"
    #: Minutes before unconfirmed enforcement reverts to audit. 0 disables the safety timer, which
    #: is only sensible once you are certain the list is right.
    confirm_minutes: int = Field(default=15, ge=0, le=1440)


class GroupInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: str | None = None
    description: str = ""
    azure_connection_id: str | None = None
    enabled: bool = True
    never_stop: bool = False


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    azure_connection_id: str | None = None
    enabled: bool | None = None
    never_stop: bool | None = None


class GroupMove(BaseModel):
    parent_id: str | None = None
    sequence: int | None = Field(default=None, ge=0)


class GroupReorder(BaseModel):
    parent_id: str | None = None
    ordered_ids: list[str] = Field(min_length=1)


class VmBulkAdd(BaseModel):
    vm_resource_ids: list[str] = Field(min_length=1, max_length=500)
    azure_connection_id: str | None = None
    enabled: bool = True
    notes: str = ""


class VmNameResolveInput(BaseModel):
    """Bare VM names to look up across a tenant, optionally narrowed to specific subscriptions."""

    names: list[str] = Field(min_length=1, max_length=500)
    subscription_ids: list[str] = Field(default_factory=list)


class VmLookupInput(BaseModel):
    """Names or resource IDs to match against the local inventory."""

    names: list[str] = Field(min_length=1, max_length=1000)


class VmPowerScanInput(BaseModel):
    """Inventory rows to read the live Azure power state for."""

    vm_ids: list[str] = Field(min_length=1, max_length=500)


class VmPowerActionInput(BaseModel):
    """An on-demand start or stop of hand-picked machines.

    Stops require the caller to echo the machine count, because this is the one path that can
    take production down without a schedule behind it.
    """

    vm_ids: list[str] = Field(min_length=1, max_length=500)
    action: Literal["start", "stop"]
    stop_mode: Literal["deallocate", "power_off"] = "deallocate"
    stagger_seconds: int = Field(default=0, ge=0, le=3600)
    confirm_count: int | None = None


class VmPatch(BaseModel):
    group_id: str | None = None
    display_name: str | None = Field(default=None, max_length=200)
    azure_connection_id: str | None = None
    enabled: bool | None = None
    never_stop: bool | None = None
    notes: str | None = None


class VmBulkAction(BaseModel):
    vm_ids: list[str] = Field(min_length=1, max_length=1000)
    action: Literal["move", "enable", "disable", "delete"]
    group_id: str | None = None


class GroupView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    parent_id: str | None
    name: str
    description: str
    path: str
    depth: int
    sequence: int
    kind: str
    azure_connection_id: str | None
    enabled: bool
    never_stop: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime


class VmView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    group_id: str
    vm_resource_id: str
    display_name: str
    subscription_id: str
    resource_group: str
    vm_name: str
    azure_connection_id: str | None
    enabled: bool
    notes: str
    never_stop: bool
    last_power_state: str
    last_power_state_at: UtcDatetime | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    schedule_id: str | None
    schedule_name: str
    action: str
    stop_mode: str
    scheduled_for: UtcDatetime | None
    started_at: UtcDatetime | None
    finished_at: UtcDatetime | None
    status: str
    mode: str
    trigger: str
    triggered_by: str | None
    total_count: int
    succeeded_count: int
    failed_count: int
    skipped_count: int
    created_at: UtcDatetime


class ScheduleInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    action: Literal["start", "stop"] = "start"
    stop_mode: Literal["deallocate", "power_off"] = "deallocate"
    ring_order: Literal["sequence", "reverse"] = "sequence"
    schedule_type: Literal["one_time", "daily", "weekly", "cron"]
    start_time: str = ""
    cron_expression: str = Field(default="", max_length=200)
    weekday: int | None = Field(default=None, ge=0, le=6)
    timezone: str | None = None
    start_date: str = Field(default="", max_length=10)
    end_date: str = Field(default="", max_length=10)
    run_limit: int | None = Field(default=None, ge=1, le=100_000)
    target_type: Literal["group", "vm"] = "group"
    target_id: str = Field(min_length=1)
    stagger_seconds: int = Field(default=0, ge=0, le=3600)
    azure_connection_id: str | None = None
    enabled: bool = True
    notes: str = ""


class SchedulePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    action: Literal["start", "stop"] | None = None
    stop_mode: Literal["deallocate", "power_off"] | None = None
    ring_order: Literal["sequence", "reverse"] | None = None
    schedule_type: Literal["one_time", "daily", "weekly", "cron"] | None = None
    start_time: str | None = None
    cron_expression: str | None = Field(default=None, max_length=200)
    weekday: int | None = Field(default=None, ge=0, le=6)
    timezone: str | None = None
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    run_limit: int | None = Field(default=None, ge=1, le=100_000)
    target_type: Literal["group", "vm"] | None = None
    target_id: str | None = Field(default=None, min_length=1)
    stagger_seconds: int | None = Field(default=None, ge=0, le=3600)
    azure_connection_id: str | None = None
    enabled: bool | None = None
    notes: str | None = None


class RecurrencePreviewInput(BaseModel):
    """Just the calendar parts of a schedule, for the live preview while editing."""

    schedule_type: Literal["one_time", "daily", "weekly", "cron"]
    start_time: str = ""
    cron_expression: str = Field(default="", max_length=200)
    weekday: int | None = Field(default=None, ge=0, le=6)
    timezone: str | None = None
    start_date: str = Field(default="", max_length=10)
    end_date: str = Field(default="", max_length=10)
    run_limit: int | None = Field(default=None, ge=1, le=100_000)
    run_count: int = Field(default=0, ge=0)


class ScheduleView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    action: str
    stop_mode: str
    ring_order: str
    schedule_type: str
    start_time: str
    cron_expression: str
    weekday: int | None
    timezone: str
    start_date: str
    end_date: str
    run_limit: int | None
    run_count: int
    target_type: str
    target_id: str
    stagger_seconds: int
    azure_connection_id: str | None
    enabled: bool
    notes: str
    status: str
    next_run_at: UtcDatetime | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class AttemptView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    schedule_id: str | None
    run_id: str | None
    vm_id: str | None
    vm_resource_id: str
    connection_id: str | None
    action: str
    stop_mode: str
    status: str
    mode: str
    message: str
    attempt_number: int
    sequence: int
    correlation_id: str
    claimed_at: UtcDatetime
    started_at: UtcDatetime | None
    completed_at: UtcDatetime | None


class CsvCommitRequest(BaseModel):
    filename: str = "import.csv"
    rows: list[dict[str, Any]]
    preview_token: str
    reject_all: bool = True


class ConnectionInput(BaseModel):
    id: str | None = None
    display_name: str = Field(min_length=1, max_length=200)
    tenant_id: str = ""
    auth_method: Literal["azure_cli", "default_chain", "service_principal", "service_principal_cert", "az_cli_token"] = "azure_cli"
    client_id: str | None = None
    client_secret: str | None = None
    certificate_pem: str | None = None
    access_token_json: str | None = None
    default_subscription: str | None = None
    allow_vm_start: bool | None = None
    allow_vm_stop: bool | None = None
    read_only: bool | None = None
    disabled: bool | None = None
    is_default: bool | None = None


class SettingsImportRequest(BaseModel):
    document: dict[str, Any]
    mode: Literal["merge", "replace"] = "merge"
    sections: list[str] | None = None


class EstateResetRequest(BaseModel):
    confirm: str = Field(max_length=20)


class AuditView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor_id: str | None
    action: str
    target_type: str
    target_id: str | None
    detail: str
    created_at: UtcDatetime


Severity = Literal["info", "warning", "error", "critical"]


class ConnectorInput(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=40)
    mode: str | None = Field(default=None, max_length=40)
    disabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)


def _validate_hhmm(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ValueError("Quiet hours must be HH:MM") from exc
    return parsed.strftime("%H:%M")


class NotificationRuleInput(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    event_types: list[str] = Field(default_factory=list, max_length=40)
    min_severity: Severity = "warning"
    scope_group_id: str | None = None
    include_subtree: bool = True
    connector_ids: list[str] = Field(default_factory=list, max_length=40)
    in_app: bool = True
    digest_mode: Literal["immediate", "per_vm", "daily"] = "immediate"
    digest_hour: int = Field(default=8, ge=0, le=23)
    digest_timezone: str = Field(default="America/New_York", max_length=100)
    quiet_hours_start: str | None = ""
    quiet_hours_end: str | None = ""
    quiet_hours_timezone: str = Field(default="America/New_York", max_length=100)
    critical_ignores_quiet_hours: bool = True
    throttle_minutes: int = Field(default=0, ge=0, le=10080)

    @field_validator("digest_timezone", "quiet_hours_timezone")
    @classmethod
    def zone_is_valid(cls, value: str) -> str:
        return validate_timezone(value)

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def quiet_hours_are_valid(cls, value: str | None) -> str:
        # Quiet hours are optional; treat null the same as "not set".
        return _validate_hhmm(value or "")

    @field_validator("event_types")
    @classmethod
    def event_types_are_known(cls, value: list[str]) -> list[str]:
        from .notifications import EVENT_TYPES

        unknown = sorted(set(value) - set(EVENT_TYPES))
        if unknown:
            raise ValueError(f"Unknown event type(s): {', '.join(unknown)}")
        return list(dict.fromkeys(value))


class NotificationRuleView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    enabled: bool
    event_types: list[str]
    min_severity: str
    scope_group_id: str | None
    include_subtree: bool
    connector_ids: list[str]
    in_app: bool
    digest_mode: str
    digest_hour: int
    digest_timezone: str
    quiet_hours_start: str
    quiet_hours_end: str
    quiet_hours_timezone: str
    critical_ignores_quiet_hours: bool
    throttle_minutes: int
    last_digest_at: UtcDatetime | None
    created_by: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class NotificationEventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: str
    severity: str
    title: str
    body: str
    facts_json: dict[str, Any]
    schedule_id: str | None
    run_id: str | None
    vm_id: str | None
    group_id: str | None
    connection_id: str | None
    fingerprint: str | None
    read: bool
    created_at: UtcDatetime


class NotificationDeliveryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_id: str
    connector_id: str
    connector_label: str
    status: str
    attempts: int
    next_attempt_at: UtcDatetime | None
    detail: str
    external_ref: str
    created_at: UtcDatetime
    sent_at: UtcDatetime | None
