export type User = { id: string; username: string; email?: string | null; role: string; auth_source?: string; must_change_password: boolean; disabled?: boolean; is_break_glass?: boolean; permissions: string[] }
export type ManagedUser = User & { created_at: string; last_login_at: string | null; locked_until: string | null }
export type LoginSession = { id:string; user_id:string; username:string; auth_method:string; created_at:string; last_seen_at:string; expires_at:string; revoked_at:string|null; ip_address:string|null; user_agent:string|null }

/* ---------------------------------------------------------------- access control */

/** A user as the access-control page sees them, with their role and group assignments resolved. */
export type AccessUser = {
  id: string
  username: string
  email: string | null
  /** Cached "most privileged assigned role", for display. `role_ids` is the real assignment. */
  role: string
  role_ids: string[]
  access_group_ids: string[]
  auth_source: string
  disabled: boolean
  is_break_glass: boolean
  must_change_password: boolean
  created_at: string
  last_login_at: string | null
  locked_until: string | null
}

export type AccessRole = {
  id: string
  name: string
  description: string
  /** Built-in roles cannot be renamed or deleted, but their permissions are still visible. */
  is_system: boolean
  /** Permission keys, or `['*']` meaning every permission. */
  permissions: string[]
}

/** A bundle of roles granted to every member. Nothing to do with the application/ring hierarchy. */
export type AccessGroup = {
  id: string
  name: string
  description: string
  role_ids: string[]
  member_count: number
}

export type PermissionCatalogItem = { key: string; label: string; group: string }

export type AuthPolicies = {
  local_login_enabled: boolean
  password_min_length: number
  password_require_upper: boolean
  password_require_lower: boolean
  password_require_number: boolean
  password_require_symbol: boolean
  lockout_attempts: number
  lockout_minutes: number
  session_idle_minutes: number
  session_absolute_hours: number
  ip_lockout_enabled: boolean
  ip_lockout_attempts: number
  ip_lockout_window_seconds: number
  ip_lockout_seconds: number
  allow_self_registration: boolean
}

/** `entra` is OIDC with the issuer derived from a directory id; `oidc` is any other issuer. */
export type ProviderType = 'entra' | 'oidc' | 'saml'

export type IdentityProvider = {
  id: string
  name: string
  type: ProviderType
  enabled: boolean
  button_label: string
  config: Record<string, unknown>
  has_client_secret: boolean
}

/** A provider offered on the sign-in page, from the public /auth/config endpoint. */
export type SignInProvider = { id: string; name: string; type: ProviderType; button_label: string; start_url: string }

export type IdpTestCheck = { name: string; ok: boolean; critical: boolean; detail: string }
export type IdpTestResult = { ok: boolean; summary: string; checks: IdpTestCheck[] }

/** Standard list envelope returned by /vms, /schedules, /runs and /groups/{id}/vms. */
export type Paged<T> = { items: T[]; total: number; limit: number; offset: number }

export type ConnectionRef = { connection_name: string | null; connection_tenant_id: string | null }

/** Group payload as returned by create/patch/move and by group detail. */
export type Group = {
  id: string
  parent_id: string | null
  name: string
  description: string
  path: string
  depth: number
  sequence: number
  /** 'application' at depth 0, 'ring' deeper. */
  kind: string
  azure_connection_id: string | null
  enabled: boolean
  /** Stop waves skip this group and everything beneath it. Starts are unaffected. */
  never_stop: boolean
  created_at: string
  updated_at: string
  name_path: string
  effective_enabled: boolean
  effective_connection_id?: string | null
  effective_connection_name?: string | null
  effective_connection_tenant_id?: string | null
  connection_inherited?: boolean
} & ConnectionRef

/** Group as returned by GET /groups (tree or flat) — carries counts and children. */
export type GroupNode = Group & {
  vm_count: number
  subtree_vm_count: number
  schedule_count: number
  subtree_schedule_count?: number
  next_run_at?: string | null
  subtree_next_run_at?: string | null
  children: GroupNode[]
}

export type GroupDetail = { group: Group; ancestors: Group[]; vms: VirtualMachine[]; schedules: Schedule[] }

export type VirtualMachine = {
  id: string
  group_id: string
  vm_resource_id: string
  display_name: string
  subscription_id: string
  resource_group: string
  vm_name: string
  azure_connection_id: string | null
  enabled: boolean
  /** Stop waves can never touch this machine — set here or inherited from an ancestor group. */
  never_stop: boolean
  notes: string
  created_at: string
  updated_at: string
  group_path: string
  effective_connection_id: string | null
  effective_connection_name: string | null
  effective_connection_tenant_id?: string | null
  /** True when never_stop is set on this VM or on any group above it. */
  stop_protected?: boolean
} & ConnectionRef

/** One row of an on-demand Azure power-state scan. Never stored — it is live state. */
export type PowerStateResult = {
  vm_id: string
  vm_name: string
  power_state: string | null
  status: 'ok' | 'not_found' | 'error'
  message: string
} & Partial<ConnectionRef>

export type PowerStateScan = {
  checked_at: string
  requested: number
  scanned: number
  failed: number
  items: PowerStateResult[]
}

export type ScheduleType = 'one_time' | 'daily' | 'weekly' | 'cron'
/** What the frequency picker offers. 'advanced' and 'cron' both persist as schedule_type 'cron'. */
export type RecurrenceFrequency = 'one_time' | 'daily' | 'weekly' | 'advanced' | 'cron'
export type TargetType = 'group' | 'vm'
/** What a wave does to its machines. Start and stop are resolved and gated independently. */
export type ScheduleAction = 'start' | 'stop'
/** Deallocate frees the host and stops compute billing; power off leaves it allocated and billed. */
export type StopMode = 'deallocate' | 'power_off'
/** Stops normally unwind the rings last-first, mirroring the start order. */
export type RingOrder = 'sequence' | 'reverse'

export type Schedule = {
  id: string
  name: string
  action: ScheduleAction
  stop_mode: StopMode
  ring_order: RingOrder
  schedule_type: ScheduleType
  start_time: string
  /** Five-field cron, used when schedule_type is 'cron'. */
  cron_expression: string
  /** 0 = Monday .. 6 = Sunday, weekly only. */
  weekday: number | null
  timezone: string
  /** Local calendar bounds in the schedule's own timezone; '' means unbounded. */
  start_date: string
  end_date: string
  /** Stop after this many scheduler-triggered runs. Manual runs never spend the budget. */
  run_limit: number | null
  run_count: number
  target_type: TargetType
  target_id: string
  stagger_seconds: number
  azure_connection_id: string | null
  enabled: boolean
  notes: string
  status: string
  next_run_at: string | null
  created_at: string
  updated_at: string
  /** Present on view payloads (list/detail); absent on the bare create/update response. */
  target_label?: string
  /** Resolved on the list payload: VMs this schedule actually acts on, after nearer schedules shadow it. */
  vm_count?: number
  connection_name?: string | null
  connection_tenant_id?: string | null
}

export type ScheduleDetail = { schedule: Schedule; vms: VirtualMachine[]; attempts: Attempt[]; runs: ScheduleRun[] }

/** POST /api/schedules/preview — the server owns cron and DST, so the editor asks rather than guesses. */
export type RecurrencePreview = {
  valid: boolean
  error: string
  description: string
  cron: string
  next_run_at: string | null
  upcoming: string[]
}

export type ScheduleRun = {
  id: string
  schedule_id: string | null
  schedule_name: string
  action: ScheduleAction
  stop_mode: StopMode
  scheduled_for: string | null
  started_at: string | null
  finished_at: string | null
  status: string
  mode: string
  trigger: string
  triggered_by: string | null
  total_count: number
  succeeded_count: number
  failed_count: number
  skipped_count: number
  created_at: string
} & ConnectionRef

/** One line of the run activity log: a wave lifecycle event or a per-VM start attempt. */
export type ActivityEvent = {
  id: string
  at: string
  kind: string
  severity: 'info' | 'success' | 'warning' | 'error'
  title: string
  summary: string
  run_id: string | null
  attempt_id?: string
  schedule_name?: string
  status: string
  mode: string
} & Partial<ConnectionRef>

export type ActivityResponse = { from: string; to: string; events: ActivityEvent[]; truncated: boolean }

export type Attempt = {
  id: string
  schedule_id: string | null
  run_id: string | null
  vm_id: string | null
  vm_resource_id: string
  connection_id: string | null
  action: ScheduleAction
  stop_mode: StopMode
  status: string
  mode: string
  message: string
  attempt_number: number
  sequence: number
  correlation_id: string
  claimed_at: string
  started_at: string | null
  completed_at: string | null
} & ConnectionRef

export type Connection = { id: string; display_name: string; tenant_id: string; auth_method: string; client_id?: string; default_subscription?: string; allow_vm_start: boolean; allow_vm_stop: boolean; read_only:boolean; disabled: boolean; is_default: boolean; status: string; status_detail?: string; token_expires_at?:string; client_secret_hint?: string | null; has_client_secret: boolean; has_certificate_pem: boolean; has_access_token_json: boolean }

export type DiscoveredVm = { id: string; name: string; resource_group: string; location: string; power_state: string | null; already_imported: boolean }
export type DiscoveryResult = { subscription_id: string; count: number; items: DiscoveredVm[] }

/** One candidate Azure VM found for a pasted bare name. */
export type ResolvedVmMatch = {
  vm_resource_id: string
  name: string
  resource_group: string
  subscription_id: string
  subscription_name: string | null
  location: string
  already_imported: boolean
  group_path: string | null
}
export type ResolvedVmName = { query: string; status: 'resolved' | 'ambiguous' | 'not_found'; matches: ResolvedVmMatch[] }
export type VmNameResolution = {
  source: 'resource_graph' | 'subscription_scan'
  requested: number
  resolved: number
  ambiguous: number
  not_found: number
  items: ResolvedVmName[]
}

export type TimelineBlock = { schedule_id: string; name: string; action: ScheduleAction; stop_mode: StopMode; start: string; end: string; group_path: string; vm_count: number; stagger_seconds: number } & ConnectionRef
export type UpcomingSchedule = { schedule_id: string; name: string; action: ScheduleAction; stop_mode: StopMode; next_run_at: string | null; timezone: string; stagger_seconds: number; target_type: TargetType; target_id: string; group_path: string; vm_count: number } & ConnectionRef

export type GeneralSettings = { app_name: string; environment: string; real_azure_starts_enabled: boolean; real_azure_stops_enabled: boolean; default_timezone: string; server_time: string; password_policy: Record<string, unknown> }
export type HealthResponse = { status: string; service: string; server_time: string }

/** GET /api/dashboard */
export type DashboardData = {
  schedule_count: number
  enabled_count: number
  group_count: number
  application_count: number
  ring_count: number
  vm_count: number
  enabled_vm_count: number
  failed_attempts: number
  running_runs: number
  failed_runs: number
  late_start_count: number
  next_schedule: Schedule | null
  recent_attempts: Attempt[]
  recent_runs: ScheduleRun[]
}

/* ---------------------------------------------------------------- operations overview */

export type Delta = { current: number; previous: number; change: number }
export type CheckSeverity = 'info' | 'warning' | 'error'

export type ReadinessCheck = { id: string; severity: CheckSeverity; title: string; detail: string; link: string }
export type TrendBucket = { start: string; runs: number; succeeded: number; failed: number; vms: number }

export type RolloutWave = {
  schedule_id: string
  name: string
  target: string
  action: ScheduleAction
  stop_mode: StopMode
  sequence: number
  next_run_at: string | null
  timezone: string
  vm_count: number
  stagger_seconds: number
  finishes_at: string | null
}

export type RolloutPlan = { id: string; name: string; waves: RolloutWave[]; vm_count: number; starts_at: string | null; finishes_at: string | null }

export type ApplicationHealth = {
  id: string
  name: string
  enabled: boolean
  vm_count: number
  covered_vm_count: number
  ring_count: number
  recent: { run_id: string; status: string; at: string; succeeded: number; failed: number; total: number }[]
  failed_runs: number
  total_runs: number
}

export type Offender = { vm_id: string | null; vm_name: string; group_path: string; failures: number; last_message: string; last_at: string | null; run_id: string | null }

/** GET /api/overview */
export type Overview = {
  window: { from: string; to: string; previous_from: string }
  generated_at: string
  estate: {
    application_count: number
    ring_count: number
    vm_count: number
    enabled_vm_count: number
    schedule_count: number
    enabled_schedule_count: number
  }
  kpis: { runs: Delta; failed_runs: Delta; failed_attempts: Delta; vms_started: Delta; running_runs: number; late_starts: number }
  trend: TrendBucket[]
  reliability: {
    runs_finished: number
    run_success_rate: number | null
    vm_success_rate: number | null
    median_seconds_to_running: number | null
    p95_seconds_to_running: number | null
    median_lateness_seconds: number | null
    worst_lateness_seconds: number | null
  }
  readiness: ReadinessCheck[]
  coverage: {
    uncovered_vm_count: number
    uncovered_sample: { id: string; vm_name: string; group_path: string }[]
    disabled_in_scheduled_ring: number
    applications_without_schedules: { id: string; name: string; vm_count: number }[]
    empty_schedules: { id: string; name: string; action: ScheduleAction }[]
    /** Machines a start wave brings up that no stop wave ever brings down — they bill until someone notices. */
    starts_but_never_stops: number
    starts_but_never_stops_sample: { id: string; vm_name: string; group_path: string }[]
    /** Machines a stop wave brings down that no start wave ever brings back. */
    stops_but_never_starts: number
    stops_but_never_starts_sample: { id: string; vm_name: string; group_path: string }[]
    stop_protected: number
  }
  power: { counts: Record<string, number>; never_scanned: number; last_scan_at: string | null }
  applications: ApplicationHealth[]
  rollout_plan: RolloutPlan[]
  offenders: Offender[]
}

/** GET /api/runs/{id} */
export type RunDetail = { run: ScheduleRun; attempts: Attempt[] }

/** One row of a CSV preview; `data` is posted back verbatim on commit. */
export type ImportRow = { row_number: number; valid: boolean; errors: string[]; resolved_from_name?: boolean; data: Record<string, unknown> }

/** POST /api/imports/preview — the inventory (v2) fields are absent for the legacy schedule format. */
export type ImportPreview = {
  filename: string
  preview_token: string
  format: 'inventory' | 'schedules'
  rows: ImportRow[]
  total: number
  valid: number
  invalid: number
  default_connection_id?: string | null
  groups_to_create?: { path: string; kind: string }[]
  applications_to_create?: number
  rings_to_create?: number
  vms_to_create?: number
  resolved_from_names?: number
  names_needing_resolution?: number
}

export type ImportCommitResult = { batch_id: string; format: string; accepted: number; rejected: number; groups_created?: number; errors: { row: number; error: string }[] }

/** Sections of the portable settings document, in the order the importer applies them. */
export type BackupSection =
  | 'azure_connections'
  | 'connectors'
  | 'groups'
  | 'virtual_machines'
  | 'schedules'
  | 'notification_rules'
  | 'security_policy'
  | 'identity_provider'
  | 'roles'
  | 'access_groups'

/** GET /api/admin/export — cross-references are carried by name, never by id, and never carry secrets. */
export type SettingsDocument = { format: string; version: number; exported_at: string; app_version: string } & Record<string, unknown>

export type BackupSectionSummary = { created: number; skipped: number; failed: number; details: { outcome: 'created' | 'skipped' | 'failed'; message: string }[] }

/** POST /api/admin/import and /api/admin/import/preview. */
export type SettingsImportSummary = {
  mode: 'merge' | 'replace'
  dry_run: boolean
  sections: Partial<Record<BackupSection, BackupSectionSummary>>
  created: number
  skipped: number
  failed: number
  needs_secret: string[]
  removed: Partial<EstateResetResult>
}

/** POST /api/admin/reset-estate */
export type EstateResetResult = { groups_removed: number; vms_removed: number; schedules_removed: number; runs_removed: number }

/** Sample Zava estate. `loaded` drives whether Settings offers to add or to remove it. */
export type DemoDataStatus = { loaded: boolean; applications: number; rings: number; virtual_machines: number; schedules: number }
export type DemoDataResult = { applications: number; rings: number; virtual_machines: number; schedules: number; runs?: number; status: DemoDataStatus }

export type AuditLog = { id: string; actor_id: string | null; action: string; target_type: string; target_id: string | null; detail: string; created_at: string }

/* ------------------------------------------------------------------ connectors & notifications */

export type Severity = 'info' | 'warning' | 'error' | 'critical'
export type ConnectorStatus = 'ok' | 'error' | 'unknown'
export type DigestMode = 'immediate' | 'per_vm' | 'daily'
export type DeliveryStatus = 'pending' | 'sent' | 'failed' | 'skipped'

/** One configurable input on a connector mode, as described by the backend registry. */
export type FieldSpec = {
  key: string
  label: string
  type: 'text' | 'password' | 'number' | 'checkbox' | 'textarea' | 'select' | 'url'
  placeholder: string
  secret: boolean
  optional: boolean
  help: string
  options: string[]
}

export type ConnectorTypeMeta = {
  id: string
  label: string
  description: string
  /** ServiceNow is false: a live "send test" would open a real incident. */
  allow_send_test: boolean
  modes: Record<string, FieldSpec[]>
}

/**
 * Secrets never leave the backend. Instead of the value, `config` carries a
 * `{field}_set: boolean` flag for every secret field.
 */
export type Connector = {
  id: string
  name: string
  type: string
  mode: string
  disabled: boolean
  status: ConnectorStatus
  status_detail: string
  last_tested: string | null
  created_at: string | null
  updated_at: string | null
  config: Record<string, string | number | boolean>
}

/** GET /api/connectors */
export type ConnectorsResponse = { connectors: Connector[]; types: ConnectorTypeMeta[]; event_types: string[] }

/** Payload for PUT /api/connectors. */
export type ConnectorInput = { id?: string; name: string; type: string; mode: string; disabled: boolean; config: Record<string, string | boolean> }

export type ConnectorTestResult = { ok: boolean; detail: string; connector: Connector | null }

export type NotificationRule = {
  id: string
  name: string
  enabled: boolean
  /** Empty means "every event type". */
  event_types: string[]
  min_severity: Severity
  scope_group_id: string | null
  include_subtree: boolean
  connector_ids: string[]
  in_app: boolean
  digest_mode: DigestMode
  digest_hour: number
  digest_timezone: string
  quiet_hours_start: string
  quiet_hours_end: string
  quiet_hours_timezone: string
  critical_ignores_quiet_hours: boolean
  throttle_minutes: number
  last_digest_at: string | null
  created_by: string | null
  created_at: string
  updated_at: string
}

/** Payload for PUT /api/notification-rules — `id` is absent when creating. */
export type NotificationRuleInput = Omit<NotificationRule, 'id' | 'last_digest_at' | 'created_by' | 'created_at' | 'updated_at'> & { id?: string }

/** GET /api/notification-rules */
export type NotificationRulesResponse = { rules: NotificationRule[]; event_types: string[]; connectors: Connector[] }

export type NotificationEvent = {
  id: string
  type: string
  severity: Severity
  title: string
  body: string
  facts_json: Record<string, unknown>
  schedule_id: string | null
  run_id: string | null
  vm_id: string | null
  group_id: string | null
  connection_id: string | null
  fingerprint: string | null
  read: boolean
  created_at: string
}

/** GET /api/notifications — a paged envelope that also carries the unread total. */
export type NotificationFeed = Paged<NotificationEvent> & { unread: number }

export type NotificationDelivery = {
  id: string
  event_id: string
  connector_id: string
  connector_label: string
  status: DeliveryStatus
  attempts: number
  next_attempt_at: string | null
  detail: string
  /** ServiceNow incident number, message id, or empty. */
  external_ref: string
  created_at: string
  sent_at: string | null
}
