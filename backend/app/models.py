from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    external_oid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    external_tenant_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    auth_source: Mapped[str] = mapped_column(String(20), default="local")
    role: Mapped[str] = mapped_column(String(32), default="admin")
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_break_glass: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("uq_users_external_identity", "external_tenant_id", "external_oid", unique=True),
    )


class LoginSession(Base):
    __tablename__ = "login_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    auth_method: Mapped[str] = mapped_column(String(20), default="local")
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    user: Mapped[User] = relationship()


class SecurityPolicy(Base):
    __tablename__ = "security_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    local_login_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    password_min_length: Mapped[int] = mapped_column(Integer, default=12)
    password_require_upper: Mapped[bool] = mapped_column(Boolean, default=True)
    password_require_lower: Mapped[bool] = mapped_column(Boolean, default=True)
    password_require_number: Mapped[bool] = mapped_column(Boolean, default=True)
    password_require_symbol: Mapped[bool] = mapped_column(Boolean, default=True)
    lockout_attempts: Mapped[int] = mapped_column(Integer, default=5)
    lockout_minutes: Mapped[int] = mapped_column(Integer, default=15)
    #: Per-IP brute-force protection, which trips before the per-account lockout when one source
    #: sprays many usernames. Counted over a sliding window, released automatically.
    ip_lockout_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ip_lockout_attempts: Mapped[int] = mapped_column(Integer, default=15)
    ip_lockout_window_seconds: Mapped[int] = mapped_column(Integer, default=300)
    ip_lockout_seconds: Mapped[int] = mapped_column(Integer, default=900)
    #: Off by default: administrators create accounts. Turning it on lets an SSO provider
    #: provision users without an invitation.
    allow_self_registration: Mapped[bool] = mapped_column(Boolean, default=False)
    session_idle_minutes: Mapped[int] = mapped_column(Integer, default=60)
    session_absolute_hours: Mapped[int] = mapped_column(Integer, default=12)
    schedule_missed_grace_seconds: Mapped[int] = mapped_column(Integer, default=300)
    default_timezone: Mapped[str] = mapped_column(String(100), default="America/New_York")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LoginThrottle(Base):
    """Per-IP brute-force counter.

    Complements the per-account lockout: an attacker spraying one password across many usernames
    never trips a single account's counter, but does trip this one. Persisted rather than held in
    memory so a restart cannot be used to clear it.
    """

    __tablename__ = "login_throttle"

    ip: Mapped[str] = mapped_column(String(64), primary_key=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IdentityProviderSettings(Base):
    __tablename__ = "identity_provider_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    tenant_id: Mapped[str] = mapped_column(String(100), default="")
    client_id: Mapped[str] = mapped_column(String(100), default="")
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_provision: Mapped[bool] = mapped_column(Boolean, default=False)
    default_role: Mapped[str] = mapped_column(String(32), default="viewer")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Role(Base):
    """A named bundle of permissions. System roles are seeded and cannot be deleted."""

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Permission strings from app.permissions. ``["*"]`` means every permission.
    permissions_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AccessGroup(Base):
    """A bundle of roles granted to every member.

    Deliberately NOT called ``groups``: that table is the application/ring hierarchy that schedules
    target. Anything to do with people lives under ``access_``.
    """

    __tablename__ = "access_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    #: Role ids granted to every member of this access group.
    role_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


class UserAccessGroup(Base):
    __tablename__ = "user_access_groups"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    access_group_id: Mapped[str] = mapped_column(ForeignKey("access_groups.id", ondelete="CASCADE"), primary_key=True)


class IdentityProvider(Base):
    """A configured single sign-on provider. Secrets inside config_json are Fernet-encrypted."""

    __tablename__ = "identity_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128))
    #: Only 'entra' today; the column exists so another protocol does not need a migration.
    type: Mapped[str] = mapped_column(String(16), default="entra")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    button_label: Mapped[str] = mapped_column(String(128), default="")
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Group(Base):
    """Arbitrary-depth tree. Depth 0 nodes are applications, deeper nodes are rings."""

    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    path: Mapped[str] = mapped_column(Text, default="", index=True)
    depth: Mapped[int] = mapped_column(Integer, default=0, index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    azure_connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    #: Protects every machine in the subtree from stop waves.
    never_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Set on sample data so "remove demo data" can delete exactly what it created and nothing else.
    #: Only meaningful on an application (depth 0); rings and VMs inherit it through the tree.
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (Index("ix_groups_parent_sequence", "parent_id", "sequence"),)

    @property
    def kind(self) -> str:
        return "application" if self.depth == 0 else "ring"


class VirtualMachine(Base):
    __tablename__ = "virtual_machines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    vm_resource_id: Mapped[str] = mapped_column(Text)
    normalized_resource_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    subscription_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    resource_group: Mapped[str] = mapped_column(String(200), default="")
    vm_name: Mapped[str] = mapped_column(String(200), default="")
    azure_connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    #: Protected machines are excluded from every stop wave, whatever a schedule targets.
    never_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    # Last power state read from Azure. Cached so the dashboard can summarise without calling ARM.
    last_power_state: Mapped[str] = mapped_column(String(32), default="")
    last_power_state_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(16), default="start", index=True)
    #: Only meaningful for stop schedules. Deallocate releases the host and stops compute billing.
    stop_mode: Mapped[str] = mapped_column(String(16), default="deallocate")
    #: Rings normally unwind in reverse for stops, so the canary ring is the last one down.
    ring_order: Mapped[str] = mapped_column(String(16), default="sequence")
    schedule_type: Mapped[str] = mapped_column(String(20), index=True)
    #: Wall-clock "HH:MM" for daily/weekly, an ISO datetime for one_time, unused for cron.
    start_time: Mapped[str] = mapped_column(String(64), default="")
    #: Five-field cron, used when schedule_type is 'cron'.
    cron_expression: Mapped[str] = mapped_column(String(200), default="")
    #: 0 = Monday .. 6 = Sunday, weekly only.
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(String(100), default="America/New_York")
    #: Local calendar bounds in this schedule's own timezone; "" means unbounded.
    start_date: Mapped[str] = mapped_column(String(10), default="")
    end_date: Mapped[str] = mapped_column(String(10), default="")
    #: Stop after this many scheduler-triggered runs. Manual runs never spend the budget.
    run_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    target_type: Mapped[str] = mapped_column(String(16), default="vm", index=True)
    target_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    stagger_seconds: Mapped[int] = mapped_column(Integer, default=0)
    azure_connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="scheduled")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    attempts: Mapped[list[VmAttempt]] = relationship(back_populates="schedule", passive_deletes=True)

    __table_args__ = (
        Index("ix_schedules_due", "enabled", "next_run_at", "lease_until"),
        Index("ix_schedules_target", "target_type", "target_id"),
    )


class ScheduleRun(Base):
    __tablename__ = "schedule_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    schedule_id: Mapped[str | None] = mapped_column(ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True, index=True)
    schedule_name: Mapped[str] = mapped_column(String(200), default="")
    action: Mapped[str] = mapped_column(String(16), default="start", index=True)
    stop_mode: Mapped[str] = mapped_column(String(16), default="deallocate")
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    mode: Mapped[str] = mapped_column(String(16), default="pending")
    trigger: Mapped[str] = mapped_column(String(16), default="scheduler", index=True)
    triggered_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    succeeded_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class VmAttempt(Base):
    """One VM's part of a wave. Named for the action it carries, which may be a start or a stop."""

    __tablename__ = "vm_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    schedule_id: Mapped[str | None] = mapped_column(ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("schedule_runs.id", ondelete="CASCADE"), nullable=True, index=True)
    vm_id: Mapped[str | None] = mapped_column(ForeignKey("virtual_machines.id", ondelete="SET NULL"), nullable=True, index=True)
    vm_resource_id: Mapped[str] = mapped_column(Text, default="")
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(16), default="start", index=True)
    stop_mode: Mapped[str] = mapped_column(String(16), default="deallocate")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    mode: Mapped[str] = mapped_column(String(16), default="pending")
    message: Mapped[str] = mapped_column(Text, default="")
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    correlation_id: Mapped[str] = mapped_column(String(36), default=new_id)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    schedule: Mapped[Schedule | None] = relationship(back_populates="attempts")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    filename: Mapped[str] = mapped_column(String(255))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="committed")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class NotificationEvent(Base):
    """Every published event; also the in-app feed so nothing is lost when no rule matches."""

    __tablename__ = "notification_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info", index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    facts_json: Mapped[dict] = mapped_column(JSON, default=dict)
    schedule_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    vm_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    group_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("notification_events.id", ondelete="CASCADE"), index=True)
    connector_id: Mapped[str] = mapped_column(String(36), index=True)
    connector_label: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    external_ref: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_notification_deliveries_due", "status", "next_attempt_at"),)


class NotificationRule(Base):
    __tablename__ = "notification_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    event_types: Mapped[list] = mapped_column(JSON, default=list)
    min_severity: Mapped[str] = mapped_column(String(16), default="warning")
    scope_group_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    include_subtree: Mapped[bool] = mapped_column(Boolean, default=True)
    connector_ids: Mapped[list] = mapped_column(JSON, default=list)
    in_app: Mapped[bool] = mapped_column(Boolean, default=True)
    digest_mode: Mapped[str] = mapped_column(String(16), default="immediate")
    digest_hour: Mapped[int] = mapped_column(Integer, default=8)
    digest_timezone: Mapped[str] = mapped_column(String(100), default="America/New_York")
    quiet_hours_start: Mapped[str] = mapped_column(String(5), default="")
    quiet_hours_end: Mapped[str] = mapped_column(String(5), default="")
    quiet_hours_timezone: Mapped[str] = mapped_column(String(100), default="America/New_York")
    critical_ignores_quiet_hours: Mapped[bool] = mapped_column(Boolean, default=True)
    throttle_minutes: Mapped[int] = mapped_column(Integer, default=0)
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
