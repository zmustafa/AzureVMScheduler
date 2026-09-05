import { useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router'
import { Activity, ArrowLeft, BellRing, RefreshCcw, Server } from 'lucide-react'
import { api, json } from '../api'
import { useCan } from '../auth'
import { connectionLabel, useScheduleIndex } from '../lib/queries'
import { connectorIcon, deliveryTone, eventLabel, severityMeta } from '../lib/notify'
import { formatDuration, useDisplayTimezone, useElapsed } from '../lib/time'
import { useClientSort } from '../lib/sorting'
import { CopyBtn } from '../components/Help'
import { ConfirmDialog } from '../components/Overlay'
import { SortHeader } from '../components/SortHeader'
import { StatusBadge } from '../components/StatusBadge'
import { ActionBadge } from '../components/ActionBits'
import { actionMeta } from '../lib/actions'
import { RunProgress, completedCount, countsText, isRunActive, latenessText, runDurationText } from '../components/RunProgress'
import { ExternalRef } from './DeliveriesPage'
import { Chip, EmptyState, ErrorNotice, PageHeader, Skeleton, TableSkeleton, Toggle } from '../components/Ui'
import type { Attempt, NotificationDelivery, NotificationFeed, Paged, RunDetail, ScheduleRun, VirtualMachine } from '../types'

const FAILED_ATTEMPT_STATUSES = new Set(['failed', 'timed_out'])

/** Every attempt for a run is already in memory, so this grid sorts in the browser. */
const ATTEMPT_SORTERS: Record<string, (row: Attempt) => unknown> = {
  vm: (row) => row.vm_resource_id.split('/').pop() ?? '',
  resource_group: (row) => row.vm_resource_id.split('/resourceGroups/')[1]?.split('/')[0] ?? '',
  tenant: (row) => row.connection_name ?? '',
  status: (row) => row.status,
  sequence: (row) => row.sequence,
  message: (row) => row.message,
  completed_at: (row) => row.completed_at ?? row.started_at ?? row.claimed_at,
}

/** ServiceNow incident numbers are the only external reference worth highlighting here. */
const INCIDENT_REF = /^inc\d+$/i

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return <div><dt className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</dt><dd className="mt-0.5 text-sm text-slate-800">{children}</dd></div>
}

function ElapsedFact({ run }: { run: ScheduleRun }) {
  const elapsed = useElapsed(run.started_at, 'running for')
  return <Fact label="Duration">{isRunActive(run) ? elapsed : runDurationText(run)}</Fact>
}

function offsetText(attempt: Attempt, run: ScheduleRun): string {
  const anchor = run.started_at ?? run.scheduled_for
  // Stagger is applied at dispatch, so measure from when the VM actually started, not when it was queued.
  const dispatched = attempt.started_at ?? attempt.claimed_at
  if (!anchor || !dispatched) return `#${attempt.sequence}`
  const delta = new Date(dispatched).getTime() - new Date(anchor).getTime()
  if (!Number.isFinite(delta) || delta < 1000) return `#${attempt.sequence} · first`
  return `#${attempt.sequence} · +${formatDuration(delta)}`
}

function AttemptRow({ attempt, run, vm }: { attempt: Attempt; run: ScheduleRun; vm?: VirtualMachine }) {
  const { format } = useDisplayTimezone()
  return <tr>
    <td>
      <span className="block font-medium text-slate-900">{vm?.display_name || vm?.vm_name || attempt.vm_resource_id.split('/').pop()}</span>
      <span className="block truncate text-xs text-slate-500" title={attempt.vm_resource_id}>{attempt.vm_resource_id}</span>
    </td>
    <td className="text-xs text-slate-600">{vm?.resource_group ?? attempt.vm_resource_id.split('/')[4] ?? '—'}</td>
    <td className="text-xs text-slate-600">{connectionLabel(attempt.connection_name, attempt.connection_tenant_id)}</td>
    <td><StatusBadge value={attempt.status} /></td>
    <td className="whitespace-nowrap text-xs text-slate-600">{offsetText(attempt, run)}</td>
    <td className="max-w-[18rem] text-xs text-slate-700">{attempt.message || '—'}</td>
    <td>
      <div className="flex items-center gap-1.5">
        <code className="font-mono text-[11px] text-slate-600">{attempt.correlation_id.slice(0, 8)}…</code>
        <CopyBtn value={attempt.correlation_id} label="Copy id" />
      </div>
    </td>
    <td className="whitespace-nowrap text-xs text-slate-600">
      <span className="block">Claimed {format(attempt.claimed_at)}</span>
      <span className="block">Started {format(attempt.started_at)}</span>
      <span className="block">Done {format(attempt.completed_at)}</span>
    </td>
  </tr>
}

/**
 * Links execution to what was announced about it. The deliveries endpoint filters by event,
 * not by run, so the run's events are resolved first and only those are expanded.
 */
function RunNotifications({ runId }: { runId: string }) {
  const { format } = useDisplayTimezone()

  const events = useQuery({
    queryKey: ['notifications', 'run', runId],
    queryFn: () => api<NotificationFeed>('/notifications?limit=200').then((data) => data.items.filter((item) => item.run_id === runId)),
  })
  const eventIds = useMemo(() => (events.data ?? []).map((item) => item.id).slice(0, 10), [events.data])

  const deliveries = useQuery({
    queryKey: ['deliveries', 'run', runId, eventIds.join(',')],
    enabled: eventIds.length > 0,
    queryFn: async () => {
      const pages = await Promise.all(eventIds.map((eventId) => api<Paged<NotificationDelivery>>(`/deliveries?event_id=${encodeURIComponent(eventId)}&limit=50`)))
      return pages.flatMap((page) => page.items)
    },
    refetchInterval: (query) => ((query.state.data ?? []).some((row) => row.status === 'pending') ? 5_000 : false),
  })

  const rows = deliveries.data ?? []

  return <section className="mt-6">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2">
        <h2 className="font-semibold text-slate-900">Notifications &amp; tickets</h2>
        {rows.length > 0 && <Chip icon={<BellRing size={12} />}>{rows.length}</Chip>}
      </div>
      <Link className="link text-sm" to="/notifications/deliveries">All deliveries</Link>
    </div>

    {events.isLoading ? <div className="card space-y-2"><Skeleton className="h-4 w-1/3" /><Skeleton className="h-4 w-2/3" /></div>
      : events.error ? <ErrorNotice error={events.error} />
        : (events.data ?? []).length === 0 ? <div className="card text-sm text-slate-600">This run raised no notification events. Successful runs only notify when a rule matches <strong>Run succeeded</strong>.</div>
          : <div className="surface">
            <ul className="divide-y divide-slate-200">{(events.data ?? []).map((event) => {
              const severity = severityMeta(event.severity)
              const sent = rows.filter((row) => row.event_id === event.id)
              return <li key={event.id} className="p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <Chip tone={severity.tone}>{severity.label}</Chip>
                  <span className="font-medium text-slate-900">{eventLabel(event.type)}</span>
                  <span className="text-xs text-slate-500">{format(event.created_at)}</span>
                </div>
                {event.title && <p className="mt-1 text-sm text-slate-700">{event.title}</p>}
                {sent.length === 0 ? <p className="mt-2 text-xs text-slate-500">{deliveries.isLoading ? 'Loading deliveries…' : 'Recorded in the in-app feed only — no connector matched.'}</p>
                  : <ul className="mt-2 space-y-1.5">{sent.map((row) => {
                    const Icon = connectorIcon(INCIDENT_REF.test(row.external_ref) ? 'servicenow' : '')
                    return <li key={row.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm">
                      <Icon size={14} className="shrink-0 text-slate-500" aria-hidden="true" />
                      <span className="font-medium text-slate-800">{row.connector_label || row.connector_id}</span>
                      <Chip tone={deliveryTone(row.status)}>{row.status}</Chip>
                      <span className="text-xs text-slate-500">{row.attempts} attempt{row.attempts === 1 ? '' : 's'}</span>
                      <ExternalRef value={row.external_ref} servicenow={INCIDENT_REF.test(row.external_ref)} />
                      {row.detail && <span className="w-full text-xs text-slate-600">{row.detail}</span>}
                    </li>
                  })}</ul>}
              </li>
            })}</ul>
          </div>}
  </section>
}

/** Full detail for one run: header facts, progress, and the per-VM attempt grid with a guarded retry. */
export function RunDetailPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const client = useQueryClient()
  const canRetry = useCan('schedules.write')
  const canSeeNotifications = useCan('notifications.read')
  const canReadSchedules = useCan('schedules.read')
  const canReadVms = useCan('vms.read')
  const { format } = useDisplayTimezone()
  const [failuresOnly, setFailuresOnly] = useState(false)
  const [confirming, setConfirming] = useState(false)

  const detail = useQuery({
    queryKey: ['run', id],
    queryFn: () => api<RunDetail>(`/runs/${id}`),
    refetchInterval: (query) => (query.state.data && isRunActive(query.state.data.run) ? 5_000 : false),
  })

  const scheduleIndex = useScheduleIndex(canReadSchedules)
  const run = detail.data?.run
  const schedule = useMemo(() => (run?.schedule_id ? (scheduleIndex.data?.items ?? []).find((item) => item.id === run.schedule_id) : undefined), [scheduleIndex.data, run?.schedule_id])
  const zone = schedule?.timezone

  const vms = useQuery({
    queryKey: ['schedule', run?.schedule_id, 'vms'],
    enabled: !!run?.schedule_id && canReadSchedules && canReadVms,
    queryFn: () => api<{ vms: VirtualMachine[] }>(`/schedules/${run!.schedule_id}`).then((data) => data.vms),
  })
  const vmById = useMemo(() => new Map((vms.data ?? []).map((item) => [item.id, item])), [vms.data])

  const attempts = useMemo(() => detail.data?.attempts ?? [], [detail.data?.attempts])
  const failedAttempts = useMemo(() => attempts.filter((item) => FAILED_ATTEMPT_STATUSES.has(item.status)), [attempts])
  const { rows: visible, sort, toggle } = useClientSort(
    failuresOnly ? failedAttempts : attempts,
    ATTEMPT_SORTERS,
    { key: 'sequence', direction: 'asc' },
  )
  const retryTenant = useMemo(() => {
    const names = new Set(failedAttempts.map((item) => connectionLabel(item.connection_name, item.connection_tenant_id)))
    return names.size === 1 ? [...names][0] : `${names.size} tenants`
  }, [failedAttempts])

  const retry = useMutation({
    mutationFn: () => api<ScheduleRun | null>(`/runs/${id}/retry-failed`, json('POST')),
    onSuccess: (created) => {
      setConfirming(false)
      void client.invalidateQueries({ queryKey: ['runs'] })
      void client.invalidateQueries({ queryKey: ['run', id] })
      void client.invalidateQueries({ queryKey: ['dashboard'] })
      if (created?.id) navigate(`/runs/${created.id}`)
    },
  })

  const back = <Link to="/runs" className="mb-4 inline-flex items-center gap-2 text-sm text-slate-600 hover:text-blue-700"><ArrowLeft size={16} />Runs</Link>

  if (detail.isLoading) return <>{back}<TableSkeleton columns={6} /></>
  if (detail.error) return <>{back}<ErrorNotice error={detail.error} /></>
  if (!run) return <>{back}<EmptyState icon={<Activity size={22} />} title="Run not found" description="This run no longer exists, or you do not have permission to read it." /></>

  const lateness = latenessText(run)
  const retryDisabled = !canRetry || failedAttempts.length === 0

  return <>
    {back}
    <PageHeader
      title={run.schedule_name}
      description={schedule?.target_label ?? (run.schedule_id ? 'Target no longer resolves to a group or virtual machine.' : 'Run on demand against a hand-picked set of machines.')}
      action={<div className="flex items-center gap-2">
        <ActionBadge action={run.action} stopMode={run.stop_mode} />
        <StatusBadge value={run.mode} />
        <StatusBadge value={run.status} />
        {canRetry && <button type="button" className="btn-secondary" disabled={retryDisabled || retry.isPending} onClick={() => setConfirming(true)}><RefreshCcw size={15} />Retry failed</button>}
      </div>}
    />

    {retry.error && <div className="mb-4"><ErrorNotice error={retry.error} /></div>}

    <section className="card">
      <dl className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Fact label="Planned start">{format(run.scheduled_for, zone)}</Fact>
        <Fact label="Actual start">{format(run.started_at, zone)}{lateness && <span className={`ml-2 text-xs font-semibold ${lateness.endsWith('late') ? 'text-amber-800' : 'text-slate-600'}`}>{lateness}</span>}</Fact>
        <ElapsedFact run={run} />
        <Fact label="Trigger">{run.trigger === 'scheduler' ? 'Scheduler' : `Manual${run.triggered_by ? ` · ${run.triggered_by}` : ''}`}</Fact>
      </dl>
      <div className="mt-5">
        <RunProgress run={run} />
        <p className="mt-2 text-sm text-slate-700">{countsText(run)} — {completedCount(run)} of {run.total_count} finished.</p>
      </div>
    </section>

    <section className="mt-6">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h2 className="font-semibold text-slate-900">Attempts</h2>
          <Chip icon={<Server size={12} />}>{attempts.length}</Chip>
          {failedAttempts.length > 0 && <Chip tone="danger">{failedAttempts.length} failed</Chip>}
        </div>
        <label className="flex items-center gap-2 text-sm text-slate-700">
          <Toggle checked={failuresOnly} onChange={setFailuresOnly} label="Show failures only" />
          Failures only
        </label>
      </div>

      {visible.length === 0 ? <EmptyState
        icon={<Activity size={22} />}
        title={failuresOnly ? 'No failed attempts' : 'No attempts recorded'}
        description={failuresOnly ? 'Every virtual machine in this run reached a non-failed state.' : 'The scheduler has not claimed any virtual machine for this run yet.'}
      /> : <div className="surface">
        <div className="max-h-[70vh] overflow-auto">
          <table className="u-table">
            <thead><tr>
              <SortHeader label="Virtual machine" sortKey="vm" sort={sort} onSort={toggle} />
              <SortHeader label="Resource group" sortKey="resource_group" sort={sort} onSort={toggle} />
              <SortHeader label="Tenant" sortKey="tenant" sort={sort} onSort={toggle} />
              <SortHeader label="Status" sortKey="status" sort={sort} onSort={toggle} />
              <SortHeader label="Sequence" sortKey="sequence" sort={sort} onSort={toggle} />
              <SortHeader label="Message" sortKey="message" sort={sort} onSort={toggle} />
              <SortHeader label="Correlation" />
              <SortHeader label="Timestamps" sortKey="completed_at" sort={sort} onSort={toggle} />
            </tr></thead>
            <tbody>{visible.map((attempt) => <AttemptRow key={attempt.id} attempt={attempt} run={run} vm={attempt.vm_id ? vmById.get(attempt.vm_id) : undefined} />)}</tbody>
          </table>
        </div>
      </div>}
    </section>

    {canSeeNotifications && <RunNotifications runId={id} />}

    <ConfirmDialog
      open={confirming}
      title="Retry failed virtual machines"
      confirmLabel={`Retry ${failedAttempts.length} VM${failedAttempts.length === 1 ? '' : 's'}`}
      tone="primary"
      busy={retry.isPending}
      onCancel={() => setConfirming(false)}
      onConfirm={() => retry.mutate()}
    >
      <p>A new run will {actionMeta(run.action).verb} <strong>{failedAttempts.length}</strong> virtual machine{failedAttempts.length === 1 ? '' : 's'} from <strong>{run.schedule_name}</strong>.</p>
      <p className="mt-2">Target tenant: <strong>{retryTenant}</strong>. Mode: <strong>{run.mode}</strong>.</p>
    </ConfirmDialog>
  </>
}
