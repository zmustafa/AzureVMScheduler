import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { BellRing, Layers, Pencil, Plus, Trash2 } from 'lucide-react'
import { api, json } from '../api'
import { useCan } from '../auth'
import { flattenGroups, useGroupTree } from '../lib/queries'
import { NOTIFICATION_RULES_KEY, PER_VM_EVENTS, SEVERITIES, SEVERITY_META, connectorIcon, eventLabel, severityMeta } from '../lib/notify'
import { Callout } from '../components/Help'
import { GroupPicker } from '../components/GroupPicker'
import { ConfirmDialog, Drawer } from '../components/Overlay'
import { TimezonePicker } from '../components/TimezonePicker'
import { Chip, EmptyState, ErrorNotice, Field, PageHeader, Skeleton, Toggle } from '../components/Ui'
import type { DigestMode, NotificationRule, NotificationRuleInput, NotificationRulesResponse, Severity } from '../types'

const DIGEST_OPTIONS: { value: DigestMode; label: string; hint: string }[] = [
  { value: 'immediate', label: 'Immediate', hint: 'One message per wave, as soon as the run finishes.' },
  { value: 'per_vm', label: 'Per virtual machine', hint: 'One message for every affected VM. Noisy on large waves.' },
  { value: 'daily', label: 'Daily digest', hint: 'Hold everything and send one summary at a fixed hour.' },
]

function newRule(): NotificationRuleInput {
  return {
    name: '',
    enabled: true,
    event_types: [],
    min_severity: 'warning',
    scope_group_id: null,
    include_subtree: true,
    connector_ids: [],
    in_app: true,
    digest_mode: 'immediate',
    digest_hour: 8,
    digest_timezone: 'America/New_York',
    quiet_hours_start: '',
    quiet_hours_end: '',
    quiet_hours_timezone: 'America/New_York',
    critical_ignores_quiet_hours: true,
    throttle_minutes: 0,
  }
}

function toInput(rule: NotificationRule): NotificationRuleInput {
  const { last_digest_at: _lastDigest, created_by: _createdBy, created_at: _createdAt, updated_at: _updatedAt, ...rest } = rule
  return rest
}

/** Multi-select rendered as toggleable chips — an empty selection means "match everything". */
function ChipSelect({ label, hint, options, selected, onToggle, renderLabel }: { label: string; hint: string; options: string[]; selected: string[]; onToggle: (value: string) => void; renderLabel: (value: string) => string }) {
  return <fieldset className="field">
    <legend className="text-sm font-medium text-slate-700">{label}</legend>
    <p className="text-xs text-slate-500">{hint}</p>
    <div className="mt-2 flex flex-wrap gap-2">{options.map((option) => {
      const active = selected.includes(option)
      return <button
        key={option}
        type="button"
        aria-pressed={active}
        onClick={() => onToggle(option)}
        className={`chip transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure focus-visible:ring-offset-1 ${active ? 'border-blue-300 bg-blue-100 text-blue-900' : 'border-slate-200 bg-white text-slate-600 hover:border-slate-400'}`}
      >{renderLabel(option)}</button>
    })}</div>
  </fieldset>
}

/* ---------------------------------------------------------------- page */

/** Routing rules: which events reach which connectors, for which part of the hierarchy. */
export function NotificationRulesPage() {
  const canManage = useCan('notifications.manage')
  const client = useQueryClient()
  const groups = useGroupTree()
  const [draft, setDraft] = useState<NotificationRuleInput | null>(null)
  const [removing, setRemoving] = useState<NotificationRule | null>(null)

  const query = useQuery({ queryKey: NOTIFICATION_RULES_KEY, queryFn: () => api<NotificationRulesResponse>('/notification-rules') })
  const rules = query.data?.rules ?? []
  const connectors = query.data?.connectors ?? []
  const eventTypes = query.data?.event_types ?? []
  const refresh = () => client.invalidateQueries({ queryKey: NOTIFICATION_RULES_KEY })

  const groupById = useMemo(() => new Map(flattenGroups(groups.data ?? []).map((item) => [item.id, item])), [groups.data])
  const connectorById = useMemo(() => new Map(connectors.map((item) => [item.id, item])), [connectors])

  const save = useMutation({
    mutationFn: (body: NotificationRuleInput) => api<NotificationRule>('/notification-rules', json('PUT', body)),
    onSuccess: () => { setDraft(null); void refresh() },
  })

  const remove = useMutation({
    mutationFn: (id: string) => api(`/notification-rules/${id}`, json('DELETE')),
    onSuccess: () => { setRemoving(null); void refresh() },
  })

  // Optimistic enable/disable with rollback so the dot never lies about the saved state.
  const toggle = useMutation({
    mutationFn: (rule: NotificationRule) => api<NotificationRule>('/notification-rules', json('PUT', { ...toInput(rule), enabled: !rule.enabled })),
    onMutate: async (rule) => {
      await client.cancelQueries({ queryKey: NOTIFICATION_RULES_KEY })
      const previous = client.getQueryData<NotificationRulesResponse>(NOTIFICATION_RULES_KEY)
      client.setQueryData<NotificationRulesResponse>(NOTIFICATION_RULES_KEY, (current) => current && { ...current, rules: current.rules.map((item) => (item.id === rule.id ? { ...item, enabled: !item.enabled } : item)) })
      return { previous }
    },
    onError: (_error, _rule, context) => { if (context?.previous) client.setQueryData(NOTIFICATION_RULES_KEY, context.previous) },
    onSettled: () => void refresh(),
  })

  const patch = (changes: Partial<NotificationRuleInput>) => setDraft((current) => (current ? { ...current, ...changes } : current))
  const toggleIn = (list: string[], value: string) => (list.includes(value) ? list.filter((item) => item !== value) : [...list, value])

  const header = <PageHeader
    title="Notifications"
    description="Rules decide which run events leave Azure VM Scheduler, where they go, and how loud they are."
    action={canManage && rules.length > 0 ? <button type="button" className="btn-primary" onClick={() => setDraft(newRule())}><Plus size={16} />New rule</button> : undefined}
  />

  if (query.isLoading) return <>{header}<div className="space-y-3">{[0, 1, 2].map((key) => <div key={key} className="card space-y-3"><Skeleton className="h-5 w-1/4" /><Skeleton className="h-4 w-2/3" /></div>)}</div></>
  if (query.error) return <>{header}<ErrorNotice error={query.error} /></>

  return <>
    {header}

    <div className="mb-5 space-y-3">
      <Callout title="How routing works">
        With <strong>no rules at all</strong>, every event still lands in the in-app notification feed — nothing is ever lost silently. A rule only adds outbound delivery to connectors on top of that.
      </Callout>
      <Callout tone="neutral" title="Immediate is per wave, not per VM">
        <strong>Immediate</strong> sends <em>one</em> wave-level message when a run finishes, summarising how many virtual machines succeeded and failed. Choose <strong>Per virtual machine</strong> only if you genuinely want one message for each affected VM.
      </Callout>
    </div>

    {save.error && <div className="mb-4"><ErrorNotice error={save.error} /></div>}
    {remove.error && <div className="mb-4"><ErrorNotice error={remove.error} /></div>}

    {rules.length === 0 ? <EmptyState
      icon={<BellRing size={22} />}
      title="No routing rules"
      description="Every event currently appears in the in-app feed only. Add a rule to also push failures to Teams, Slack, email or ServiceNow."
      action={canManage ? <button type="button" className="btn-primary" onClick={() => setDraft(newRule())}><Plus size={16} />New rule</button> : <Link className="link" to="/notifications/deliveries">View delivery history</Link>}
    /> : <div className="space-y-3">{rules.map((rule) => {
      const scope = rule.scope_group_id ? groupById.get(rule.scope_group_id)?.name_path ?? 'Deleted group' : 'All applications'
      const severity = severityMeta(rule.min_severity)
      return <article key={rule.id} className="card">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${rule.enabled ? 'bg-emerald-500' : 'bg-slate-300'}`} aria-hidden="true" />
              <h2 className="font-semibold text-slate-900">{rule.name}</h2>
              <Chip tone={rule.enabled ? 'success' : 'neutral'}>{rule.enabled ? 'Enabled' : 'Disabled'}</Chip>
              <Chip tone={severity.tone}>{severity.label} and above</Chip>
              <Chip tone="accent">{DIGEST_OPTIONS.find((item) => item.value === rule.digest_mode)?.label ?? rule.digest_mode}</Chip>
            </div>
            <p className="mt-2 flex items-center gap-1.5 text-sm text-slate-600"><Layers size={14} className="shrink-0 text-slate-400" />Scope: <strong className="font-medium text-slate-800">{scope}</strong>{rule.scope_group_id && <span className="text-xs text-slate-500">({rule.include_subtree ? 'including rings beneath it' : 'this group only'})</span>}</p>
          </div>
          {canManage && <div className="flex shrink-0 items-center gap-2">
            <Toggle checked={rule.enabled} onChange={() => toggle.mutate(rule)} label={`${rule.enabled ? 'Disable' : 'Enable'} ${rule.name}`} busy={toggle.isPending} />
            <button type="button" className="btn-secondary !py-1" onClick={() => setDraft(toInput(rule))}><Pencil size={14} />Edit</button>
            <button type="button" className="btn-danger !py-1" onClick={() => setRemoving(rule)}><Trash2 size={14} />Delete</button>
          </div>}
        </div>

        <dl className="mt-4 grid gap-4 border-t border-slate-200 pt-4 sm:grid-cols-2">
          <div>
            <dt className="text-xs font-semibold uppercase tracking-wide text-slate-500">Matched events</dt>
            <dd className="mt-1 flex flex-wrap gap-1.5">{rule.event_types.length === 0 ? <Chip>Every event type</Chip> : rule.event_types.map((type) => <Chip key={type} tone="info">{eventLabel(type)}</Chip>)}</dd>
          </div>
          <div>
            <dt className="text-xs font-semibold uppercase tracking-wide text-slate-500">Delivered to</dt>
            <dd className="mt-1 flex flex-wrap gap-1.5">
              {rule.in_app && <Chip icon={<BellRing size={12} />}>In-app feed</Chip>}
              {rule.connector_ids.map((id) => {
                const connector = connectorById.get(id)
                const Icon = connectorIcon(connector?.type ?? '')
                return <Chip key={id} tone={connector ? 'accent' : 'danger'} icon={<Icon size={12} />}>{connector?.name ?? 'Deleted connector'}</Chip>
              })}
              {!rule.in_app && rule.connector_ids.length === 0 && <Chip tone="warn">No targets — this rule delivers nowhere</Chip>}
            </dd>
          </div>
        </dl>
      </article>
    })}</div>}

    <Drawer
      open={!!draft}
      title={draft?.id ? 'Edit notification rule' : 'New notification rule'}
      description="Match events, choose a scope, then pick where they are delivered."
      width="max-w-2xl"
      onClose={() => setDraft(null)}
      footer={<>
        <button type="button" className="btn-secondary" onClick={() => setDraft(null)}>Cancel</button>
        <button type="button" className="btn-primary" disabled={save.isPending || !draft?.name.trim()} onClick={() => draft && save.mutate({ ...draft, name: draft.name.trim() })}>{save.isPending ? 'Saving…' : 'Save rule'}</button>
      </>}
    >
      {draft && <div className="space-y-6">
        {save.error && <ErrorNotice error={save.error} />}

        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Rule name" hint="Shown on delivery rows and in the audit log."><input value={draft.name} onChange={(event) => patch({ name: event.target.value })} required /></Field>
          <div className="field">
            <span className="text-sm font-medium text-slate-700">Enabled</span>
            <div className="flex items-center gap-3"><Toggle checked={draft.enabled} onChange={(next) => patch({ enabled: next })} label="Rule enabled" /><span className="text-sm text-slate-600">{draft.enabled ? 'Actively routing events' : 'Paused — matches nothing'}</span></div>
          </div>
        </div>

        <section className="space-y-4 border-t border-slate-200 pt-5">
          <h3 className="text-sm font-semibold text-slate-900">Match</h3>
          <ChipSelect
            label="Event types"
            hint="Leave every chip unselected to match all event types."
            options={eventTypes}
            selected={draft.event_types}
            onToggle={(value) => patch({ event_types: toggleIn(draft.event_types, value) })}
            renderLabel={(value) => `${eventLabel(value)}${PER_VM_EVENTS.has(value) ? ' (per VM)' : ''}`}
          />
          <Field label="Minimum severity" hint="Anything below this threshold is ignored by this rule.">
            <select value={draft.min_severity} onChange={(event) => patch({ min_severity: event.target.value as Severity })}>
              {SEVERITIES.map((value) => <option key={value} value={value}>{SEVERITY_META[value].label} and above</option>)}
            </select>
          </Field>
        </section>

        <section className="space-y-3 border-t border-slate-200 pt-5">
          <h3 className="text-sm font-semibold text-slate-900">Scope</h3>
          <p className="text-xs text-slate-500">Limit the rule to one application or ring, or leave it across the whole estate.</p>
          <GroupPicker
            nodes={groups.data ?? []}
            value={draft.scope_group_id}
            onChange={(id) => patch({ scope_group_id: id })}
            allowRoot
            rootLabel="All applications"
            label="Notification scope"
          />
          {draft.scope_group_id && <div className="flex items-center gap-3">
            <Toggle checked={draft.include_subtree} onChange={(next) => patch({ include_subtree: next })} label="Include rings beneath this group" />
            <span className="text-sm text-slate-700">Include every ring beneath this group</span>
          </div>}
        </section>

        <section className="space-y-4 border-t border-slate-200 pt-5">
          <h3 className="text-sm font-semibold text-slate-900">Cadence</h3>
          <Field label="Digest mode" hint={DIGEST_OPTIONS.find((item) => item.value === draft.digest_mode)?.hint}>
            <select value={draft.digest_mode} onChange={(event) => patch({ digest_mode: event.target.value as DigestMode })}>
              {DIGEST_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
          </Field>
          {draft.digest_mode === 'daily' && <div className="grid gap-4 md:grid-cols-2">
            <Field label="Send at" hint="Local hour in the digest timezone.">
              <select value={String(draft.digest_hour)} onChange={(event) => patch({ digest_hour: Number(event.target.value) })}>
                {Array.from({ length: 24 }, (_, hour) => <option key={hour} value={hour}>{String(hour).padStart(2, '0')}:00</option>)}
              </select>
            </Field>
            <Field label="Digest timezone"><TimezonePicker value={draft.digest_timezone} onChange={(zone) => patch({ digest_timezone: zone })} /></Field>
          </div>}
          <Field label="Throttle (minutes)" hint="Suppress repeats of the same event for this many minutes. 0 disables throttling.">
            <input type="number" autoComplete="off" min={0} max={10080} value={draft.throttle_minutes} onChange={(event) => patch({ throttle_minutes: Math.max(0, Number(event.target.value) || 0) })} />
          </Field>
        </section>

        <section className="space-y-4 border-t border-slate-200 pt-5">
          <h3 className="text-sm font-semibold text-slate-900">Quiet hours</h3>
          <p className="text-xs text-slate-500">Leave both blank to deliver around the clock. Overnight windows such as 22:00 → 07:00 are supported.</p>
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Start (HH:MM)"><input type="time" value={draft.quiet_hours_start} onChange={(event) => patch({ quiet_hours_start: event.target.value })} /></Field>
            <Field label="End (HH:MM)"><input type="time" value={draft.quiet_hours_end} onChange={(event) => patch({ quiet_hours_end: event.target.value })} /></Field>
          </div>
          <Field label="Quiet hours timezone"><TimezonePicker value={draft.quiet_hours_timezone} onChange={(zone) => patch({ quiet_hours_timezone: zone })} /></Field>
          <div className="flex items-center gap-3">
            <Toggle checked={draft.critical_ignores_quiet_hours} onChange={(next) => patch({ critical_ignores_quiet_hours: next })} label="Critical events ignore quiet hours" />
            <span className="text-sm text-slate-700">Critical events break through quiet hours</span>
          </div>
        </section>

        <section className="space-y-4 border-t border-slate-200 pt-5">
          <h3 className="text-sm font-semibold text-slate-900">Deliver to</h3>
          <div className="flex items-center gap-3">
            <Toggle checked={draft.in_app} onChange={(next) => patch({ in_app: next })} label="Deliver to the in-app feed" />
            <span className="text-sm text-slate-700">In-app notification feed (the bell)</span>
          </div>
          {connectors.length === 0
            ? <Callout tone="warn" title="No connectors configured">This rule can only use the in-app feed. <Link className="link" to="/settings/connectors">Add a connector</Link> to send to Teams, Slack, email or ServiceNow.</Callout>
            : <ChipSelect
              label="Connectors"
              hint="Every selected connector receives a copy of each matching event."
              options={connectors.map((item) => item.id)}
              selected={draft.connector_ids}
              onToggle={(value) => patch({ connector_ids: toggleIn(draft.connector_ids, value) })}
              renderLabel={(id) => { const connector = connectorById.get(id); return connector ? `${connector.name}${connector.disabled ? ' (disabled)' : ''}` : id }}
            />}
        </section>
      </div>}
    </Drawer>

    <ConfirmDialog
      open={!!removing}
      title="Delete notification rule"
      confirmLabel="Delete rule"
      busy={remove.isPending}
      onCancel={() => setRemoving(null)}
      onConfirm={() => removing && remove.mutate(removing.id)}
    >
      <p><strong>{removing?.name}</strong> will stop routing events to its connectors.</p>
      <p className="mt-2">Matching events keep appearing in the in-app feed.</p>
    </ConfirmDialog>
  </>
}
