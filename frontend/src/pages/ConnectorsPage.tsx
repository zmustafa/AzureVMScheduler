import { useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, HelpCircle, Pencil, Plug, PlugZap, Send, Trash2 } from 'lucide-react'
import { api, json } from '../api'
import { useCan } from '../auth'
import { CONNECTOR_CATEGORIES, connectorIcon, modeLabel, useConnectorCatalog, CONNECTORS_KEY } from '../lib/notify'
import { useDisplayTimezone } from '../lib/time'
import { Callout, CopyBtn, CopyRow, PermissionTable, SetupGuide, Step } from '../components/Help'
import { ConfirmDialog, Drawer } from '../components/Overlay'
import { Chip, EmptyState, ErrorNotice, Field, PageHeader, Skeleton, Toggle } from '../components/Ui'
import type { Connector, ConnectorInput, ConnectorTestResult, ConnectorsResponse, ConnectorTypeMeta, FieldSpec } from '../types'

type Draft = { id?: string; name: string; type: string; mode: string; disabled: boolean; values: Record<string, string | boolean> }

const STATUS_META: Record<string, { dot: string; label: string; tone: 'success' | 'danger' | 'neutral' }> = {
  ok: { dot: 'bg-emerald-500', label: 'Healthy', tone: 'success' },
  error: { dot: 'bg-rose-500', label: 'Failing', tone: 'danger' },
  unknown: { dot: 'bg-slate-400', label: 'Not tested', tone: 'neutral' },
}

function statusMeta(status: string) {
  return STATUS_META[status] ?? STATUS_META.unknown
}

/** Seed the drawer form from the stored connector; secrets are never present, only `{key}_set` flags. */
function draftFrom(connector: Connector, meta: ConnectorTypeMeta | undefined): Draft {
  const specs = meta?.modes[connector.mode] ?? []
  const values: Record<string, string | boolean> = {}
  for (const spec of specs) {
    if (spec.secret) { values[spec.key] = '' ; continue }
    const stored = connector.config[spec.key]
    values[spec.key] = spec.type === 'checkbox' ? stored === true || stored === 'true' : stored === undefined || stored === null ? '' : String(stored)
  }
  return { id: connector.id, name: connector.name, type: connector.type, mode: connector.mode, disabled: connector.disabled, values }
}

function blankDraft(meta: ConnectorTypeMeta, mode: string): Draft {
  const values: Record<string, string | boolean> = {}
  for (const spec of meta.modes[mode] ?? []) values[spec.key] = spec.type === 'checkbox' ? false : ''
  return { name: meta.label, type: meta.id, mode, disabled: false, values }
}

/* ---------------------------------------------------------------- dynamic field rendering */

function DynamicField({ spec, value, secretStored, onChange }: { spec: FieldSpec; value: string | boolean; secretStored: boolean; onChange: (next: string | boolean) => void }) {
  const hint = [spec.help, spec.optional ? 'Optional.' : ''].filter(Boolean).join(' ')
  if (spec.type === 'checkbox') {
    return <div className="field md:col-span-2">
      <div className="flex items-center gap-3">
        <Toggle checked={value === true} onChange={onChange} label={spec.label} />
        <span className="text-sm font-medium text-slate-700">{spec.label}</span>
      </div>
      {hint && <p className="text-xs text-slate-500">{hint}</p>}
    </div>
  }
  const placeholder = spec.secret && secretStored ? 'Stored securely — leave blank to keep' : spec.placeholder
  const common = {
    value: typeof value === 'string' ? value : '',
    placeholder,
    onChange: (event: { target: { value: string } }) => onChange(event.target.value),
  }
  const label = spec.optional ? `${spec.label} (optional)` : spec.label
  if (spec.type === 'select') {
    return <Field label={label} hint={hint || undefined}>
      <select value={typeof value === 'string' ? value : ''} onChange={(event) => onChange(event.target.value)}>
        <option value="">Not set</option>
        {spec.options.map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
    </Field>
  }
  if (spec.type === 'textarea') return <Field label={label} hint={hint || undefined} wide><textarea rows={3} {...common} /></Field>
  const inputType = spec.type === 'password' ? 'password' : spec.type === 'number' ? 'number' : spec.type === 'url' ? 'url' : 'text'
  const autoComplete = spec.type === 'password' ? 'new-password' : undefined
  return <Field label={label} hint={hint || undefined}><input type={inputType} autoComplete={autoComplete} {...common} /></Field>
}

/* ---------------------------------------------------------------- setup guides */

const GRAPH_PERMISSIONS = [{ name: 'Mail.Send', type: 'Application', desc: 'Lets Azure VM Scheduler send mail as the mailbox below without a signed-in user.' }] as const

function ConnectorSetupGuide({ type, mode }: { type: string; mode: string }) {
  if (type === 'email' && mode === 'smtp') return <SetupGuide title="Setup guide — SMTP relay" subtitle="host, port, TLS">
    <ol className="space-y-4">
      <Step n={1} title="Point at your relay">Use the internal relay or provider host, for example <code className="font-mono">smtp.zava.com</code>. Azure VM Scheduler opens the connection from this machine, so the relay must accept it.</Step>
      <Step n={2} title="Pick the port and the matching TLS mode">Port <strong>587</strong> with <strong>Use STARTTLS</strong> enabled is the normal choice. Port <strong>465</strong> needs <strong>Use implicit SSL</strong> instead. Port 25 with neither is plaintext and should only be used on a trusted internal relay.</Step>
      <Step n={3} title="Authenticate if the relay requires it">Username and password are optional — anonymous internal relays work with both left blank. The password is encrypted at rest and never returned to this page.</Step>
      <Step n={4} title="Set the from and to addresses">Recipients are <strong>fixed per connector</strong>: everything routed here goes to the same To/Cc list. Create one connector per distribution list rather than trying to vary recipients per event.</Step>
    </ol>
    <div className="mt-4"><Callout tone="warn" title="Sender reputation">The from address must be one the relay is allowed to send as, otherwise SPF or DMARC will silently drop the mail even though Azure VM Scheduler records a successful delivery.</Callout></div>
  </SetupGuide>

  if (type === 'email') return <SetupGuide title="Setup guide — Microsoft 365 (Graph sendMail)" subtitle="app registration">
    <ol className="space-y-4">
      <Step n={1} title="Create or reuse an app registration">In the Entra admin center open <strong>App registrations → New registration</strong>, single tenant. Copy the <strong>Application (client) ID</strong> and <strong>Directory (tenant) ID</strong>. Leave both fields blank here to reuse the registration that already backs Entra sign-in.</Step>
      <Step n={2} title="Add a client secret">Under <strong>Certificates &amp; secrets → New client secret</strong>, copy the value immediately — Entra shows it only once. Paste it below; it is encrypted at rest.</Step>
      <Step n={3} title="Grant the application permission">Under <strong>API permissions → Add a permission → Microsoft Graph → Application permissions</strong>, add <code className="font-mono">Mail.Send</code>, then click <strong>Grant admin consent</strong>. Delegated permissions will not work — there is no signed-in user.<PermissionTable rows={GRAPH_PERMISSIONS} /></Step>
      <Step n={4} title="Choose the sending mailbox">Graph sends through <code className="font-mono">/users/&#123;mailbox&#125;/sendMail</code>, so the mailbox must be a real licensed or shared mailbox in the tenant. Recipients are fixed per connector.</Step>
    </ol>
    <div className="mt-4"><Callout tone="warn" title="Scope the permission">
      <code className="font-mono">Mail.Send</code> as an application permission grants send-as rights for <em>every</em> mailbox in the tenant. Restrict it with an Exchange application access policy limited to the mailbox above.
    </Callout></div>
  </SetupGuide>

  if (type === 'teams') return <SetupGuide title="Setup guide — Microsoft Teams incoming webhook" subtitle="per channel">
    <ol className="space-y-4">
      <Step n={1} title="Open the target channel">In Teams, pick the channel that should receive run notifications, then choose <strong>⋯ → Manage channel → Connectors</strong> (or <strong>Workflows</strong> on newer tenants).</Step>
      <Step n={2} title="Configure an incoming webhook">Find <strong>Incoming Webhook</strong> and select <strong>Configure</strong>. Name it <em>Azure VM Scheduler</em> so the origin of each card is obvious in the channel.</Step>
      <Step n={3} title="Copy the URL">Teams shows the webhook URL once. Paste it below — it is a bearer credential and is encrypted at rest.</Step>
      <Step n={4} title="Send a test">Use <strong>Send test</strong> on the connector card. Azure VM Scheduler posts an Adaptive Card coloured by severity with the application, ring, schedule and VM counts.</Step>
    </ol>
    <div className="mt-4"><Callout title="One channel per webhook">The URL hard-codes the destination channel. To notify several channels, create one connector per channel and select them all on a notification rule.</Callout></div>
  </SetupGuide>

  if (type === 'slack') return <SetupGuide title="Setup guide — Slack incoming webhook" subtitle="per channel">
    <ol className="space-y-4">
      <Step n={1} title="Create a Slack app"> Go to <code className="font-mono">api.slack.com/apps</code> → <strong>Create New App → From scratch</strong>, name it <em>Azure VM Scheduler</em> and pick your workspace.</Step>
      <Step n={2} title="Turn on incoming webhooks">Open <strong>Incoming Webhooks</strong> and switch <strong>Activate Incoming Webhooks</strong> to on.</Step>
      <Step n={3} title="Add a webhook to a channel">Choose <strong>Add New Webhook to Workspace</strong>, select the destination channel and approve. Slack returns a URL of the form below.<CopyRow label="format" value="https://hooks.slack.com/services/{team-id}/{channel-id}/{secret-token}" /></Step>
      <Step n={4} title="Paste it below">The URL is the credential — anyone holding it can post to that channel, so it is stored encrypted and never shown again.</Step>
    </ol>
    <div className="mt-4"><Callout title="Message format">Azure VM Scheduler posts Block Kit messages with a severity colour bar plus fields for the application, ring, schedule and success/failure counts.</Callout></div>
  </SetupGuide>

  if (type === 'servicenow') return <SetupGuide title="Setup guide — ServiceNow incidents" subtitle="itil integration user">
    <ol className="space-y-4">
      <Step n={1} title="Create a dedicated integration user">In ServiceNow open <strong>User Administration → Users</strong> and create a service account such as <code className="font-mono">svc-azureops</code>. Mark it <strong>Web service access only</strong> so it cannot sign in interactively.</Step>
      <Step n={2} title="Grant the itil role">The account needs the <code className="font-mono">itil</code> role to create, read and update records in the <code className="font-mono">incident</code> table through <code className="font-mono">/api/now/table/incident</code>. Without it every call returns 403.<PermissionTable rows={[{ name: 'itil', type: 'Role', desc: 'Create, read and update incidents through the Table API.' }]} /></Step>
      <Step n={3} title="Use the instance URL, not a UI deep link">Enter the base instance origin only.<CopyRow label="format" value="https://zava.service-now.com" /></Step>
      <Step n={4} title="Set the defaults you want on new incidents">Urgency, impact, assignment group and caller are applied to every incident Azure VM Scheduler opens. <strong>Open incidents for</strong> narrows which event types raise a ticket — leave it blank to raise one for every routed event.</Step>
      <Step n={5} title="Understand the correlation behaviour">Azure VM Scheduler stamps each incident with a <code className="font-mono">correlation_id</code> derived from the schedule. A repeat failure <strong>updates the existing open incident</strong> with a work note instead of opening a duplicate, and with <strong>Close the incident on the next success</strong> enabled the next successful run <strong>auto-resolves</strong> it.</Step>
    </ol>
    <div className="mt-4"><Callout tone="warn" title="No live send test">ServiceNow has no dry-run path — a test send would open a real incident that a human has to close. Use <strong>Test</strong> instead: it authenticates and reads one incident without writing anything.</Callout></div>
  </SetupGuide>

  return <SetupGuide title="Setup guide — custom HTTPS webhook" subtitle="signed JSON">
    <ol className="space-y-4">
      <Step n={1} title="Expose an HTTPS endpoint">Plain HTTP is rejected. Azure VM Scheduler sends a single <code className="font-mono">POST</code> with <code className="font-mono">Content-Type: application/json</code>. Any response below <code className="font-mono">400</code> counts as delivered; <code className="font-mono">408</code>, <code className="font-mono">425</code>, <code className="font-mono">429</code> and <code className="font-mono">5xx</code> are retried with backoff, every other error is final.</Step>
      <Step n={2} title="Handle the JSON payload">Every delivery has the same top-level shape. <code className="font-mono">facts</code> carries the run context and varies by event type; <code className="font-mono">link</code> is null when the event is not tied to a run.<WebhookPayloadDocs /></Step>
      <Step n={3} title="Add headers if your gateway needs them">Custom headers are a JSON object, for example <code className="font-mono">&#123;"X-Env": "prod"&#125;</code>.</Step>
      <Step n={4} title="Verify the signature">Set a signing secret and Azure VM Scheduler signs <code className="font-mono">timestamp.nonce.body</code> with HMAC-SHA256, sending these headers. Recompute the digest over the <em>raw</em> body and reject anything that does not match or whose timestamp is stale.
        <CopyRow label="header" value="X-AzureOps-Signature: sha256=<hex digest>" />
        <CopyRow label="header" value="X-AzureOps-Timestamp: <unix seconds>" />
        <CopyRow label="header" value="X-AzureOps-Nonce: <32 hex chars>" />
      </Step>
    </ol>
  </SetupGuide>
}

const PAYLOAD_SAMPLE = `{
  "event_type": "run.partially_failed",
  "severity": "error",
  "title": "ABC app ring2 start partially failed — 24/30 succeeded · 6 failed",
  "body": "ABC app / ring2 wave finished with status partially_failed.",
  "link": "http://127.0.0.1:5173/runs/1f2e...",
  "sent_at": "2026-07-25T12:00:04.512000+00:00",
  "facts": {
    "application": "ABC app",
    "ring": "ring2",
    "schedule_name": "ABC app ring2 start",
    "scheduled_for": "2026-07-25T12:00:00+00:00",
    "vm_count": 30,
    "succeeded": 24,
    "failed": 6,
    "skipped": 0,
    "failed_vm_names": ["vm-abc-r2-07", "vm-abc-r2-11"],
    "tenant": "Zava Production",
    "run_url": "http://127.0.0.1:5173/runs/1f2e...",
    "error": "Azure start failed (409): Conflict"
  }
}`

const PAYLOAD_FIELDS = [
  { name: 'event_type', type: 'string', desc: 'run.succeeded · run.partially_failed · run.failed · run.timed_out · vm.start_failed · vm.start_timed_out · vm.start_skipped · vm.stop_failed · vm.stop_timed_out · vm.stop_skipped · schedule.missed · connection.unhealthy' },
  { name: 'severity', type: 'string', desc: 'info, warning, error or critical.' },
  { name: 'title', type: 'string', desc: 'One-line summary, already includes the succeeded/failed counts.' },
  { name: 'body', type: 'string', desc: 'Longer description naming the application and ring.' },
  { name: 'link', type: 'string | null', desc: 'Deep link back to the run in Azure VM Scheduler.' },
  { name: 'sent_at', type: 'string', desc: 'ISO-8601 UTC instant the payload was built.' },
  { name: 'facts', type: 'object', desc: 'Structured context — see the table below.' },
]

const FACT_FIELDS = [
  { name: 'application', type: 'string', desc: 'Root application name; empty when the VM is ungrouped.' },
  { name: 'ring', type: 'string', desc: 'Ring name path beneath the application; empty for application-level schedules.' },
  { name: 'schedule_name', type: 'string', desc: 'Schedule that produced the wave.' },
  { name: 'scheduled_for', type: 'string', desc: 'ISO-8601 UTC time the wave was due (absent on per-VM events).' },
  { name: 'vm_count', type: 'number', desc: 'VMs targeted by the wave; 1 on per-VM events.' },
  { name: 'succeeded', type: 'number', desc: 'VMs confirmed running.' },
  { name: 'failed', type: 'number', desc: 'VMs that failed or timed out.' },
  { name: 'skipped', type: 'number', desc: 'VMs skipped, for example an unavailable tenant (wave events only).' },
  { name: 'failed_vm_names', type: 'string[]', desc: 'Names of the VMs that did not start.' },
  { name: 'tenant', type: 'string', desc: 'Azure connection display name used for the attempt.' },
  { name: 'run_url', type: 'string', desc: 'Same value as the top-level link; derived from the configured app base URL.' },
  { name: 'error', type: 'string', desc: 'First sanitized failure message; empty on success.' },
]

/** Documents the exact JSON contract so an endpoint can be written without reading our source. */
function WebhookPayloadDocs() {
  return <div className="mt-2 space-y-3">
    <div>
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Example body</span>
        <CopyBtn value={PAYLOAD_SAMPLE} label="Copy JSON" />
      </div>
      <pre className="max-h-72 overflow-auto rounded border border-slate-200 bg-white px-2 py-1.5 font-mono text-[11px] leading-5 text-slate-800">{PAYLOAD_SAMPLE}</pre>
    </div>
    <div>
      <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Top level</p>
      <PermissionTable columns={['Field', 'Type', 'Meaning']} rows={PAYLOAD_FIELDS.map((item) => ({ name: item.name, type: item.type, desc: item.desc }))} />
    </div>
    <div>
      <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">facts</p>
      <PermissionTable columns={['Fact', 'Type', 'Meaning']} rows={FACT_FIELDS.map((item) => ({ name: item.name, type: item.type, desc: item.desc }))} />
    </div>
    <Callout tone="neutral" title="Stability">
      Fields are only ever added, never renamed or removed, so parse defensively and ignore keys you do not recognise. A fact is omitted when it has no value for that event type.
    </Callout>
  </div>
}

/* ---------------------------------------------------------------- gallery */

function Gallery({ types, onPick }: { types: ConnectorTypeMeta[]; onPick: (meta: ConnectorTypeMeta) => void }) {
  const byId = new Map(types.map((item) => [item.id, item]))
  const categorised = new Set(CONNECTOR_CATEGORIES.flatMap((item) => item.types))
  const extra = types.filter((item) => !categorised.has(item.id))
  const sections = [...CONNECTOR_CATEGORIES.map((section) => ({ ...section, items: section.types.map((id) => byId.get(id)).filter((item): item is ConnectorTypeMeta => !!item) })), ...(extra.length ? [{ title: 'More', blurb: 'Other integrations exposed by this deployment.', items: extra }] : [])]
  return <div className="space-y-8">{sections.filter((section) => section.items.length > 0).map((section) => <section key={section.title}>
    <h3 className="text-sm font-semibold text-slate-900">{section.title}</h3>
    <p className="mt-0.5 text-xs text-slate-500">{section.blurb}</p>
    <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{section.items.map((meta) => {
      const Icon = connectorIcon(meta.id)
      return <button key={meta.id} type="button" onClick={() => onPick(meta)} className="group flex flex-col items-start gap-2 rounded-xl border border-slate-200 bg-white p-4 text-left shadow-sm transition hover:border-blue-300 hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure focus-visible:ring-offset-2">
        <span className="grid h-10 w-10 place-items-center rounded-lg bg-blue-50 text-blue-700"><Icon size={20} /></span>
        <span className="font-semibold text-slate-900">{meta.label}</span>
        <span className="text-xs leading-relaxed text-slate-600">{meta.description}</span>
        <span className="mt-auto pt-2 text-xs font-semibold text-blue-700 opacity-0 transition group-hover:opacity-100 group-focus-visible:opacity-100">Set up {meta.label} →</span>
      </button>
    })}</div>
  </section>)}</div>
}

/* ---------------------------------------------------------------- page */

/** Connector inventory: cards with health and per-connector actions, plus a metadata-driven editor. */
export function ConnectorsPage() {
  const canManage = useCan('connectors.manage')
  const client = useQueryClient()
  const { format } = useDisplayTimezone()
  const catalog = useConnectorCatalog()
  const [draft, setDraft] = useState<Draft | null>(null)
  const [picking, setPicking] = useState(false)
  const [removing, setRemoving] = useState<Connector | null>(null)
  const [notice, setNotice] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const types = catalog.data?.types ?? []
  const connectors = catalog.data?.connectors ?? []
  const typeById = useMemo(() => new Map(types.map((item) => [item.id, item])), [types])
  const refresh = () => client.invalidateQueries({ queryKey: CONNECTORS_KEY })

  const save = useMutation({
    mutationFn: (body: ConnectorInput) => api<Connector>('/connectors', json('PUT', body)),
    onSuccess: () => { setDraft(null); setNotice({ tone: 'ok', text: 'Connector saved.' }); void refresh() },
  })

  const probe = useMutation({
    mutationFn: ({ id, live }: { id: string; live: boolean }) => api<ConnectorTestResult>(`/connectors/${id}/${live ? 'send-test' : 'test'}`, json('POST')),
    onMutate: ({ id }) => { setBusyId(id); setNotice(null) },
    onSuccess: (result) => { setNotice({ tone: 'ok', text: result.detail || 'Connector reachable.' }); void refresh() },
    onError: (error) => { setNotice({ tone: 'bad', text: error instanceof Error ? error.message : 'The connector could not be reached.' }); void refresh() },
    onSettled: () => setBusyId(null),
  })

  const remove = useMutation({
    mutationFn: (id: string) => api(`/connectors/${id}`, json('DELETE')),
    onSuccess: () => { setRemoving(null); setNotice({ tone: 'ok', text: 'Connector deleted.' }); void refresh() },
  })

  // Optimistic enable/disable: flip the cached card immediately, roll the cache back if the PUT fails.
  const toggle = useMutation({
    mutationFn: (connector: Connector) => api<Connector>('/connectors', json('PUT', { id: connector.id, name: connector.name, type: connector.type, mode: connector.mode, disabled: !connector.disabled, config: {} })),
    onMutate: async (connector) => {
      await client.cancelQueries({ queryKey: CONNECTORS_KEY })
      const previous = client.getQueryData<ConnectorsResponse>(CONNECTORS_KEY)
      client.setQueryData<ConnectorsResponse>(CONNECTORS_KEY, (current) => current && { ...current, connectors: current.connectors.map((item) => (item.id === connector.id ? { ...item, disabled: !item.disabled } : item)) })
      return { previous }
    },
    onError: (error, _connector, context) => {
      if (context?.previous) client.setQueryData(CONNECTORS_KEY, context.previous)
      setNotice({ tone: 'bad', text: error instanceof Error ? error.message : 'Could not change the connector state.' })
    },
    onSettled: () => void refresh(),
  })

  const submit = () => {
    if (!draft) return
    const specs = typeById.get(draft.type)?.modes[draft.mode] ?? []
    const config: Record<string, string | boolean> = {}
    for (const spec of specs) {
      const value = draft.values[spec.key]
      if (spec.secret && value === '') continue // blank keeps the stored secret
      config[spec.key] = spec.type === 'checkbox' ? value === true : String(value ?? '')
    }
    save.mutate({ id: draft.id, name: draft.name.trim(), type: draft.type, mode: draft.mode, disabled: draft.disabled, config })
  }

  const startCreate = (meta: ConnectorTypeMeta) => {
    setPicking(false)
    setDraft(blankDraft(meta, Object.keys(meta.modes)[0] ?? ''))
  }

  const draftMeta = draft ? typeById.get(draft.type) : undefined
  const draftSpecs = draftMeta?.modes[draft?.mode ?? ''] ?? []
  const storedSecrets = useMemo(() => {
    const stored = draft?.id ? connectors.find((item) => item.id === draft.id)?.config ?? {} : {}
    return (key: string) => stored[`${key}_set`] === true
  }, [connectors, draft?.id])

  const header = <PageHeader
    title="Connectors"
    description="Where Azure VM Scheduler sends notifications — chat, email, ticketing, or your own endpoint. Secrets are encrypted at rest and never sent back to the browser."
    action={canManage && connectors.length > 0 ? <button type="button" className="btn-primary" onClick={() => setPicking(true)}><Plug size={16} />Add connector</button> : undefined}
  />

  if (catalog.isLoading) return <>{header}<div className="grid gap-4 xl:grid-cols-2">{[0, 1, 2, 3].map((key) => <div key={key} className="card space-y-3"><Skeleton className="h-5 w-1/3" /><Skeleton className="h-4 w-2/3" /><Skeleton className="h-8 w-full" /></div>)}</div></>
  if (catalog.error) return <>{header}<ErrorNotice error={catalog.error} /></>

  return <>
    {header}

    {notice && <div className={`mb-4 rounded-lg border p-3 text-sm ${notice.tone === 'ok' ? 'border-emerald-200 bg-emerald-50 text-emerald-800' : 'border-rose-200 bg-rose-50 text-rose-800'}`} role="status">
      <span className="inline-flex items-center gap-2">{notice.tone === 'ok' ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}{notice.text}</span>
    </div>}
    {save.error && <div className="mb-4"><ErrorNotice error={save.error} /></div>}
    {remove.error && <div className="mb-4"><ErrorNotice error={remove.error} /></div>}

    {connectors.length === 0 ? <div className="space-y-6">
      <EmptyState
        icon={<PlugZap size={22} />}
        title="No connectors yet"
        description={canManage ? 'Pick an integration below. Until one exists, notifications only appear in the in-app feed.' : 'No integrations are configured. Notifications only appear in the in-app feed.'}
      />
      {canManage && <div className="card"><Gallery types={types} onPick={startCreate} /></div>}
    </div> : <div className="grid gap-4 xl:grid-cols-2">{connectors.map((connector) => {
      const Icon = connectorIcon(connector.type)
      const meta = typeById.get(connector.type)
      const status = statusMeta(connector.status)
      const liveTestBlocked = meta ? !meta.allow_send_test : connector.type === 'servicenow'
      return <article key={connector.id} className="card flex flex-col gap-3">
        <div className="flex items-start gap-3">
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-blue-50 text-blue-700"><Icon size={20} /></span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="truncate font-semibold text-slate-900">{connector.name}</h2>
              <Chip>{meta?.label ?? connector.type} · {modeLabel(connector.type, connector.mode)}</Chip>
              {connector.disabled && <Chip tone="neutral">Disabled</Chip>}
            </div>
            <p className="mt-1 flex items-center gap-2 text-sm text-slate-600">
              <span className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${status.dot}`} aria-hidden="true" />
              <span className="font-medium text-slate-700">{status.label}</span>
              <span className="truncate">{connector.status_detail || 'No test has been run yet.'}</span>
            </p>
            {connector.last_tested && <p className="mt-0.5 text-xs text-slate-500">Last tested {format(connector.last_tested)}</p>}
          </div>
          {canManage && <div className="flex shrink-0 items-center gap-2">
            <Toggle checked={!connector.disabled} onChange={() => toggle.mutate(connector)} label={`${connector.disabled ? 'Enable' : 'Disable'} ${connector.name}`} busy={toggle.isPending} />
          </div>}
        </div>
        {canManage && <div className="flex flex-wrap gap-2 border-t border-slate-200 pt-3">
          <button type="button" className="btn-secondary !py-1" disabled={busyId === connector.id} onClick={() => probe.mutate({ id: connector.id, live: false })}>Test</button>
          {liveTestBlocked
            ? <button type="button" className="btn-secondary !py-1" disabled title="Disabled for ServiceNow: a live test would open a real incident that somebody has to close. Use Test instead — it authenticates without writing."><Send size={14} />Send test<HelpCircle size={13} /></button>
            : <button type="button" className="btn-secondary !py-1" disabled={busyId === connector.id} onClick={() => probe.mutate({ id: connector.id, live: true })}><Send size={14} />Send test</button>}
          <button type="button" className="btn-secondary !py-1" onClick={() => setDraft(draftFrom(connector, meta))}><Pencil size={14} />Edit</button>
          <button type="button" className="btn-danger !py-1" onClick={() => setRemoving(connector)}><Trash2 size={14} />Delete</button>
        </div>}
      </article>
    })}</div>}

    <Drawer open={picking} title="Add a connector" description="Choose the integration you want Azure VM Scheduler to notify." width="max-w-3xl" onClose={() => setPicking(false)}>
      <Gallery types={types} onPick={startCreate} />
    </Drawer>

    <Drawer
      open={!!draft}
      title={draft?.id ? 'Edit connector' : `New ${draftMeta?.label ?? ''} connector`}
      description={draftMeta?.description}
      width="max-w-2xl"
      onClose={() => setDraft(null)}
      footer={<>
        <button type="button" className="btn-secondary" onClick={() => setDraft(null)}>Cancel</button>
        <button type="button" className="btn-primary" disabled={save.isPending || !draft?.name.trim()} onClick={submit}>{save.isPending ? 'Saving…' : 'Save connector'}</button>
      </>}
    >
      {draft && draftMeta && <div className="space-y-5">
        <ConnectorSetupGuide type={draft.type} mode={draft.mode} />
        {save.error && <ErrorNotice error={save.error} />}
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Display name" hint="Shown on delivery rows and in notification rules."><input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} required /></Field>
          {Object.keys(draftMeta.modes).length > 1 && <Field label="Mode" hint="Changing the mode replaces this connector's stored settings.">
            <select value={draft.mode} onChange={(event) => setDraft({ ...blankDraft(draftMeta, event.target.value), id: draft.id, name: draft.name, disabled: draft.disabled })}>
              {Object.keys(draftMeta.modes).map((mode) => <option key={mode} value={mode}>{modeLabel(draftMeta.id, mode)}</option>)}
            </select>
          </Field>}
        </div>
        <div className="grid gap-4 border-t border-slate-200 pt-5 md:grid-cols-2">
          {draftSpecs.map((spec) => <DynamicField
            key={spec.key}
            spec={spec}
            value={draft.values[spec.key] ?? (spec.type === 'checkbox' ? false : '')}
            secretStored={storedSecrets(spec.key)}
            onChange={(next) => setDraft({ ...draft, values: { ...draft.values, [spec.key]: next } })}
          />)}
        </div>
        <div className="flex items-center gap-3 border-t border-slate-200 pt-5">
          <Toggle checked={!draft.disabled} onChange={(next) => setDraft({ ...draft, disabled: !next })} label="Connector enabled" />
          <span className="text-sm text-slate-700">Enabled — a disabled connector is skipped by every rule.</span>
        </div>
      </div>}
    </Drawer>

    <ConfirmDialog
      open={!!removing}
      title="Delete connector"
      confirmLabel="Delete connector"
      busy={remove.isPending}
      onCancel={() => setRemoving(null)}
      onConfirm={() => removing && remove.mutate(removing.id)}
    >
      <p><strong>{removing?.name}</strong> will be removed along with its stored credentials.</p>
      <p className="mt-2">Notification rules that target it will stop delivering there. Past delivery records are kept for the audit trail.</p>
    </ConfirmDialog>
  </>
}
