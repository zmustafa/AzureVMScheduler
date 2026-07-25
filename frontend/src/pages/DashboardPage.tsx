import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router'
import { Activity, AlarmClock, CircleAlert, Layers, Pause, Play, Power, RefreshCcw, Server, TriangleAlert } from 'lucide-react'
import { api, json } from '../api'
import { useCan } from '../auth'
import { connectionLabel } from '../lib/queries'
import { actionMeta } from '../lib/actions'
import { useCountdown, useDisplayTimezone, useElapsed, useTick, formatDuration, serverNow, zoneLabel } from '../lib/time'
import { DEFAULT_RANGE, resolveRange, type TimeRange } from '../lib/timeRange'
import { StatusBadge } from '../components/StatusBadge'
import { TimeRangePicker } from '../components/TimeRangePicker'
import { ReadinessStrip } from '../components/ReadinessStrip'
import { Sparkline } from '../components/Sparkline'
import { HealthMatrix, RolloutPlanPanel } from '../components/OverviewPanels'
import { RunProgress, countsText, isRunActive, isRunFailed, latenessText, runDurationText } from '../components/RunProgress'
import { Chip, ErrorNotice, PageHeader, Skeleton, TableSkeleton } from '../components/Ui'
import type { ActivityResponse, Overview, Paged, ScheduleRun, TimelineBlock, UpcomingSchedule } from '../types'

const POLL_MS = 15_000
const PIN_KEY = 'azureops.pinned-applications'

type Kpi = {
  label: string
  value: string
  hint: string
  to: string
  icon: typeof Server
  tone: 'neutral' | 'warn' | 'danger'
  delta?: { change: number; previous: number; goodWhenDown?: boolean }
  trend?: number[]
  trendTone?: 'blue' | 'rose' | 'emerald'
}

function kpisFor(data: Overview): Kpi[] {
  const { estate, kpis, trend } = data
  return [
    {
      label: 'Waves', value: String(kpis.runs.current), hint: 'in this window', to: '/runs', icon: Activity, tone: 'neutral',
      delta: { change: kpis.runs.change, previous: kpis.runs.previous }, trend: trend.map((item) => item.runs), trendTone: 'blue',
    },
    {
      label: 'VMs started', value: String(kpis.vms_started.current), hint: 'reached running', to: '/runs', icon: Power, tone: 'neutral',
      delta: { change: kpis.vms_started.change, previous: kpis.vms_started.previous }, trend: trend.map((item) => item.vms), trendTone: 'emerald',
    },
    {
      label: 'Failed waves', value: String(kpis.failed_runs.current), hint: 'in this window', to: '/runs?status=failed', icon: TriangleAlert,
      tone: kpis.failed_runs.current ? 'danger' : 'neutral',
      delta: { change: kpis.failed_runs.change, previous: kpis.failed_runs.previous, goodWhenDown: true },
      trend: trend.map((item) => item.failed), trendTone: 'rose',
    },
    {
      label: 'Failed VMs', value: String(kpis.failed_attempts.current), hint: 'start and stop attempts', to: '/runs?status=failed', icon: CircleAlert,
      tone: kpis.failed_attempts.current ? 'danger' : 'neutral',
      delta: { change: kpis.failed_attempts.change, previous: kpis.failed_attempts.previous, goodWhenDown: true },
    },
    { label: 'Running now', value: String(kpis.running_runs), hint: 'in flight', to: '/runs?status=running', icon: Activity, tone: kpis.running_runs ? 'warn' : 'neutral' },
    { label: 'Late starts', value: String(kpis.late_starts), hint: 'past the grace period', to: '/schedules', icon: AlarmClock, tone: kpis.late_starts ? 'danger' : 'neutral' },
    { label: 'Applications', value: String(estate.application_count), hint: `${estate.ring_count} ring${estate.ring_count === 1 ? '' : 's'}`, to: '/applications', icon: Layers, tone: 'neutral' },
    { label: 'Managed VMs', value: `${estate.enabled_vm_count}/${estate.vm_count}`, hint: `${estate.enabled_schedule_count} enabled schedules`, to: '/vms', icon: Server, tone: 'neutral' },
  ]
}

const TONE_RING = { neutral: 'hover:border-blue-300', warn: 'border-amber-200 hover:border-amber-300', danger: 'border-rose-200 hover:border-rose-300' } as const
const TONE_TEXT = { neutral: 'text-slate-500', warn: 'text-amber-700', danger: 'text-rose-700' } as const

function DeltaBadge({ delta }: { delta: NonNullable<Kpi['delta']> }) {
  if (delta.change === 0) return <span className="text-xs text-slate-500">no change</span>
  const worse = delta.goodWhenDown ? delta.change > 0 : false
  const better = delta.goodWhenDown ? delta.change < 0 : delta.change > 0
  const tone = worse ? 'text-rose-700' : better ? 'text-emerald-700' : 'text-slate-600'
  return <span className={`text-xs font-semibold ${tone}`} title={`Previous period: ${delta.previous}`}>
    {delta.change > 0 ? '▲' : '▼'} {Math.abs(delta.change)} vs previous
  </span>
}

function KpiCard({ kpi }: { kpi: Kpi }) {
  const Icon = kpi.icon
  return <Link to={kpi.to} className={`card block transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure focus-visible:ring-offset-2 ${TONE_RING[kpi.tone]}`}>
    <div className="flex items-start justify-between gap-2">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{kpi.label}</p>
      <Icon size={18} className={TONE_TEXT[kpi.tone]} aria-hidden="true" />
    </div>
    <div className="mt-2 flex items-end justify-between gap-2">
      <p className="text-2xl font-semibold tabular-nums text-slate-900">{kpi.value}</p>
      {kpi.trend && <Sparkline values={kpi.trend} tone={kpi.trendTone} label={`${kpi.label} trend`} />}
    </div>
    <div className="mt-0.5 flex flex-wrap items-center gap-2">
      <p className="text-xs text-slate-500">{kpi.hint}</p>
      {kpi.delta && <DeltaBadge delta={kpi.delta} />}
    </div>
  </Link>
}

function UpcomingRow({ wave }: { wave: UpcomingSchedule }) {
  const { format } = useDisplayTimezone()
  const countdown = useCountdown(wave.next_run_at, wave.timezone)
  const overdue = !!wave.next_run_at && new Date(wave.next_run_at).getTime() < Date.now()
  return <li className={`rounded-lg border p-3 ${overdue ? 'border-amber-300 bg-amber-50' : 'border-slate-200 bg-white'}`}>
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div className="min-w-0">
        <Link className="link block truncate" to={`/schedules/${wave.schedule_id}`}>{wave.name}</Link>
        <p className="truncate text-xs text-slate-500">{wave.group_path || (wave.target_type === 'vm' ? 'Single virtual machine' : 'No target resolved')}</p>
      </div>
      <div className="text-right">
        <p className="text-sm text-slate-800">{format(wave.next_run_at, wave.timezone)}</p>
        <p className={`text-xs font-semibold ${overdue ? 'text-amber-800' : 'text-blue-700'}`}>{overdue ? `Overdue — ${countdown}` : countdown}</p>
      </div>
    </div>
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      <Chip icon={<Server size={12} />}>{wave.vm_count} VM{wave.vm_count === 1 ? '' : 's'}</Chip>
      <Chip tone="info">{zoneLabel(wave.timezone, wave.next_run_at)}</Chip>
      {wave.stagger_seconds > 0 && <Chip>{wave.stagger_seconds}s stagger</Chip>}
      <Chip tone="accent">{connectionLabel(wave.connection_name, wave.connection_tenant_id)}</Chip>
    </div>
  </li>
}

function LiveRunRow({ run }: { run: ScheduleRun }) {
  const elapsed = useElapsed(run.started_at ?? run.created_at)
  return <li className="rounded-lg border border-slate-200 bg-white p-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <Link className="link truncate" to={`/runs/${run.id}`}>{run.schedule_name}</Link>
      <div className="flex items-center gap-2"><StatusBadge value={run.mode} /><StatusBadge value={run.status} /></div>
    </div>
    <div className="mt-2"><RunProgress run={run} /></div>
    <p className="mt-1.5 text-xs text-slate-600">{countsText(run)} · {elapsed}</p>
  </li>
}

function RecentRunRow({ run, onRetry, canRetry, retrying }: { run: ScheduleRun; onRetry: (id: string) => void; canRetry: boolean; retrying: boolean }) {
  const { format } = useDisplayTimezone()
  const lateness = latenessText(run)
  return <li className="flex flex-wrap items-center justify-between gap-3 py-3">
    <div className="min-w-0">
      <Link className="link block truncate" to={`/runs/${run.id}`}>{run.schedule_name}</Link>
      <p className="text-xs text-slate-500">{format(run.started_at ?? run.scheduled_for ?? run.created_at)}{lateness ? ` · ${lateness}` : ''} · {runDurationText(run)}</p>
      <p className="text-xs text-slate-600">{countsText(run)}</p>
    </div>
    <div className="flex shrink-0 items-center gap-2">
      <StatusBadge value={run.status} />
      {canRetry && isRunFailed(run) && run.failed_count > 0 && <button type="button" className="btn-secondary !px-2 !py-1 text-xs" disabled={retrying} onClick={() => onRetry(run.id)}><RefreshCcw size={13} />Retry failed</button>}
    </div>
  </li>
}

function Panel({ title, description, children, action }: { title: string; description?: string; children: ReactNode; action?: ReactNode }) {
  // mb-6 stands in for the grid gap, and break-inside-avoid keeps a card whole when the panels
  // flow into columns.
  return <section className="card mb-6 break-inside-avoid">
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div><h2 className="font-semibold text-slate-900">{title}</h2>{description && <p className="muted">{description}</p>}</div>
      {action}
    </div>
    <div className="mt-4">{children}</div>
  </section>
}

/** Horizontal density band for the next 24 hours: reveals waves that collide on the same tenant. */
function TimelineStrip({ blocks }: { blocks: TimelineBlock[] }) {
  const { format } = useDisplayTimezone()
  useTick(60_000)
  const now = serverNow()
  const end = now + 24 * 3_600_000
  const visible = blocks.filter((block) => new Date(block.start).getTime() <= end)

  if (visible.length === 0) return <p className="py-6 text-center muted">Nothing is scheduled in the next 24 hours.</p>

  const peak = Math.max(...visible.map((block) => block.vm_count), 1)
  return <div>
    <div className="relative h-16 rounded bg-slate-50">
      {visible.map((block) => {
        const start = new Date(block.start).getTime()
        const left = Math.max(((start - now) / (end - now)) * 100, 0)
        const meta = actionMeta(block.action)
        return <div
          key={`${block.schedule_id}-${block.start}`}
          className={`absolute bottom-0 w-1.5 -translate-x-1/2 rounded-t transition hover:opacity-70 ${meta.bar}`}
          style={{ left: `${left}%`, height: `${Math.max((block.vm_count / peak) * 100, 12)}%` }}
          title={`${meta.label}: ${block.name} — ${block.vm_count} VM${block.vm_count === 1 ? '' : 's'} · ${format(block.start)}`}
        />
      })}
      <div className="absolute inset-y-0 left-0 w-px bg-slate-300" aria-hidden="true" />
    </div>
    <div className="mt-1 flex justify-between text-[11px] text-slate-500">
      <span>now</span><span>+6h</span><span>+12h</span><span>+18h</span><span>+24h</span>
    </div>
    <div className="mt-2 flex flex-wrap gap-3 text-[11px] text-slate-600">
      {(['start', 'stop'] as const).map((option) => {
        const meta = actionMeta(option)
        return <span key={option} className="inline-flex items-center gap-1">
          <span className={`h-2 w-2 rounded-sm ${meta.bar}`} aria-hidden="true" />{meta.label}s
        </span>
      })}
    </div>
  </div>
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return <div>
    <dt className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</dt>
    <dd className="mt-0.5 text-lg font-semibold tabular-nums text-slate-900">{value}</dd>
    {hint && <p className="text-xs text-slate-500">{hint}</p>}
  </div>
}

const seconds = (value: number | null) => (value === null ? '—' : formatDuration(value * 1000))
const percent = (value: number | null) => (value === null ? '—' : `${Math.round(value * 100)}%`)

/** Everything that will silently not happen, plus the machines that keep failing. */
function AttentionPanel({ data }: { data: Overview }) {
  const { coverage, offenders } = data
  const { format } = useDisplayTimezone()
  const nothing = coverage.uncovered_vm_count === 0
    && coverage.disabled_in_scheduled_ring === 0
    && coverage.applications_without_schedules.length === 0
    && coverage.empty_schedules.length === 0
    && coverage.starts_but_never_stops === 0
    && coverage.stops_but_never_starts === 0
    && offenders.length === 0

  if (nothing) return <p className="py-8 text-center muted">Every virtual machine is covered by a schedule and nothing is failing repeatedly.</p>

  return <div className="space-y-4">
    {coverage.uncovered_vm_count > 0 && <div>
      <p className="text-sm font-semibold text-slate-900">{coverage.uncovered_vm_count} virtual machine{coverage.uncovered_vm_count === 1 ? '' : 's'} are not covered by any schedule</p>
      <p className="text-xs text-slate-600">They will never be started or stopped automatically.</p>
      <ul className="mt-1.5 flex flex-wrap gap-1.5">
        {coverage.uncovered_sample.map((vm) => <li key={vm.id}><Chip title={vm.group_path}>{vm.vm_name}</Chip></li>)}
        {coverage.uncovered_vm_count > coverage.uncovered_sample.length && <li><Link className="link text-xs" to="/vms">and {coverage.uncovered_vm_count - coverage.uncovered_sample.length} more</Link></li>}
      </ul>
    </div>}

    {/* The expensive gap: something brings these up every day and nothing ever brings them down. */}
    {coverage.starts_but_never_stops > 0 && <div>
      <p className="text-sm font-semibold text-slate-900">{coverage.starts_but_never_stops} virtual machine{coverage.starts_but_never_stops === 1 ? ' is started but never stopped' : 's are started but never stopped'}</p>
      <p className="text-xs text-slate-600">They keep billing until someone stops them by hand.</p>
      <ul className="mt-1.5 flex flex-wrap gap-1.5">
        {coverage.starts_but_never_stops_sample.map((vm) => <li key={vm.id}><Chip tone="warn" title={vm.group_path}>{vm.vm_name}</Chip></li>)}
        {coverage.starts_but_never_stops > coverage.starts_but_never_stops_sample.length && <li><Link className="link text-xs" to="/schedules">and {coverage.starts_but_never_stops - coverage.starts_but_never_stops_sample.length} more</Link></li>}
      </ul>
    </div>}

    {/* The dangerous gap: something takes these down and nothing brings them back. */}
    {coverage.stops_but_never_starts > 0 && <div>
      <p className="text-sm font-semibold text-slate-900">{coverage.stops_but_never_starts} virtual machine{coverage.stops_but_never_starts === 1 ? ' is stopped but never started' : 's are stopped but never started'}</p>
      <p className="text-xs text-slate-600">Once a stop wave runs, nothing brings them back automatically.</p>
      <ul className="mt-1.5 flex flex-wrap gap-1.5">
        {coverage.stops_but_never_starts_sample.map((vm) => <li key={vm.id}><Chip tone="danger" title={vm.group_path}>{vm.vm_name}</Chip></li>)}
        {coverage.stops_but_never_starts > coverage.stops_but_never_starts_sample.length && <li><Link className="link text-xs" to="/schedules">and {coverage.stops_but_never_starts - coverage.stops_but_never_starts_sample.length} more</Link></li>}
      </ul>
    </div>}

    {coverage.applications_without_schedules.length > 0 && <div>
      <p className="text-sm font-semibold text-slate-900">{coverage.applications_without_schedules.length} application{coverage.applications_without_schedules.length === 1 ? ' has' : 's have'} no enabled schedule</p>
      <ul className="mt-1.5 flex flex-wrap gap-1.5">
        {coverage.applications_without_schedules.map((app) => <li key={app.id}><Link to={`/applications/${app.id}`}><Chip tone="warn">{app.name} · {app.vm_count} VMs</Chip></Link></li>)}
      </ul>
    </div>}

    {coverage.empty_schedules.length > 0 && <div>
      <p className="text-sm font-semibold text-slate-900">{coverage.empty_schedules.length} enabled schedule{coverage.empty_schedules.length === 1 ? '' : 's'} resolve to zero VMs</p>
      <ul className="mt-1.5 flex flex-wrap gap-1.5">
        {coverage.empty_schedules.map((item) => <li key={item.id}><Link to={`/schedules/${item.id}`}><Chip tone="warn">{item.name} · {actionMeta(item.action).label}</Chip></Link></li>)}
      </ul>
    </div>}

    {coverage.stop_protected > 0 && <p className="text-sm text-slate-700">
      <span className="font-semibold">{coverage.stop_protected}</span> virtual machine{coverage.stop_protected === 1 ? ' is' : 's are'} marked never stop and will be skipped by every stop wave.
    </p>}

    {coverage.disabled_in_scheduled_ring > 0 && <p className="text-sm text-slate-700">
      <span className="font-semibold">{coverage.disabled_in_scheduled_ring}</span> disabled virtual machine{coverage.disabled_in_scheduled_ring === 1 ? '' : 's'} sit inside a scheduled ring and will be skipped.
    </p>}

    {offenders.length > 0 && <div>
      <p className="text-sm font-semibold text-slate-900">Repeatedly failing machines</p>
      <ul className="mt-1.5 divide-y divide-slate-100">
        {offenders.map((item) => <li key={item.vm_name} className="flex flex-wrap items-center justify-between gap-2 py-1.5">
          <div className="min-w-0">
            <p className="truncate text-sm text-slate-800">{item.vm_name} <span className="text-xs text-slate-500">{item.group_path}</span></p>
            <p className="truncate text-xs text-slate-600">{item.last_message || 'No message recorded'}{item.last_at ? ` · ${format(item.last_at)}` : ''}</p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Chip tone="danger">{item.failures} failure{item.failures === 1 ? '' : 's'}</Chip>
            {item.run_id && <Link className="link text-xs" to={`/runs/${item.run_id}`}>Last run</Link>}
          </div>
        </li>)}
      </ul>
    </div>}
  </div>
}

/** A fresh install has nothing to show, so point at the first three things to do instead. */
function Onboarding({ data }: { data: Overview }) {
  const steps = [
    { done: false, label: 'Connect an Azure tenant', to: '/settings/tenants', hint: 'Store credentials encrypted on this host.' },
    { done: data.estate.vm_count > 0, label: 'Add virtual machines', to: '/import', hint: 'Import a CSV or discover them from a tenant.' },
    { done: data.estate.schedule_count > 0, label: 'Create a schedule', to: '/schedules', hint: 'Target an application or a single ring.' },
  ]
  return <div className="card">
    <h2 className="font-semibold text-slate-900">Get started</h2>
    <p className="muted">Three steps to your first automated start wave.</p>
    <ol className="mt-4 space-y-2">
      {steps.map((step, index) => <li key={step.label} className="flex items-center gap-3 rounded-lg border border-slate-200 p-3">
        <span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${step.done ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-100 text-slate-600'}`}>{step.done ? '✓' : index + 1}</span>
        <div className="min-w-0 flex-1">
          <Link className="link font-medium" to={step.to}>{step.label}</Link>
          <p className="text-xs text-slate-500">{step.hint}</p>
        </div>
      </li>)}
    </ol>
  </div>
}

/** Enterprise operations overview: readiness, windowed metrics, the rollout plan and live activity. */
export function DashboardPage() {
  const canRetry = useCan('schedules.write')
  const client = useQueryClient()
  const navigate = useNavigate()
  const { format } = useDisplayTimezone()
  const [range, setRange] = useState<TimeRange>(DEFAULT_RANGE)
  const [paused, setPaused] = useState(false)
  const [pinned, setPinned] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem(PIN_KEY) ?? '[]') as string[] } catch { return [] }
  })

  const togglePin = useCallback((id: string) => {
    setPinned((current) => {
      const next = current.includes(id) ? current.filter((item) => item !== id) : [...current, id]
      try { localStorage.setItem(PIN_KEY, JSON.stringify(next)) } catch { /* storage is optional */ }
      return next
    })
  }, [])

  // Only re-resolve the window every 30s so the query keys stay stable between renders.
  useTick(30_000)
  const anchor = Math.floor(serverNow() / 30_000) * 30_000
  const window = useMemo(() => resolveRange(range, anchor), [range, anchor])
  const poll = paused ? false : POLL_MS

  const overviewPath = useMemo(
    () => `/overview?from=${encodeURIComponent(new Date(window.from).toISOString())}&to=${encodeURIComponent(new Date(window.to).toISOString())}`,
    [window.from, window.to],
  )
  const overview = useQuery({ queryKey: ['overview', overviewPath], queryFn: () => api<Overview>(overviewPath), refetchInterval: poll, placeholderData: (previous) => previous })
  const upcoming = useQuery({ queryKey: ['schedules', 'upcoming', 8], queryFn: () => api<UpcomingSchedule[]>('/schedules/upcoming?limit=8'), refetchInterval: poll })
  const runs = useQuery({ queryKey: ['runs', 'dashboard'], queryFn: () => api<Paged<ScheduleRun>>('/runs?limit=50'), refetchInterval: poll })
  const timeline = useQuery({
    queryKey: ['timeline', 'dashboard', anchor],
    queryFn: () => api<TimelineBlock[]>(`/timeline?from=${new Date(anchor).toISOString()}&to=${new Date(anchor + 24 * 3_600_000).toISOString()}`),
    refetchInterval: poll,
    placeholderData: (previous) => previous,
  })
  const activity = useQuery({
    queryKey: ['runs-activity', 'dashboard', overviewPath],
    queryFn: () => api<ActivityResponse>(`/runs/activity?from=${encodeURIComponent(new Date(window.from).toISOString())}&to=${encodeURIComponent(new Date(window.to).toISOString())}&limit=40`),
    refetchInterval: poll,
    placeholderData: (previous) => previous,
  })

  const retry = useMutation({
    mutationFn: (runId: string) => api<ScheduleRun | null>(`/runs/${runId}/retry-failed`, json('POST')),
    onSuccess: (created) => {
      void client.invalidateQueries({ queryKey: ['runs'] })
      void client.invalidateQueries({ queryKey: ['overview'] })
      if (created?.id) navigate(`/runs/${created.id}`)
    },
  })

  const items = useMemo(() => runs.data?.items ?? [], [runs.data])
  const live = useMemo(() => items.filter(isRunActive), [items])
  const failures = useMemo(() => items.filter(isRunFailed).slice(0, 6), [items])
  const recent = useMemo(() => items.slice(0, 6), [items])

  if (overview.error) return <ErrorNotice error={overview.error} />

  const data = overview.data
  const power = data?.power
  const powerTotal = power ? Object.values(power.counts).reduce((sum, value) => sum + value, 0) : 0
  const isFresh = !!data && data.estate.vm_count === 0 && data.estate.schedule_count === 0

  return <>
    <PageHeader
      title="Operations overview"
      description="Readiness, windowed metrics, tonight's rollout plan and everything running right now."
      action={<div className="flex flex-wrap items-center gap-2">
        <TimeRangePicker value={range} onChange={setRange} now={anchor} />
        <button
          type="button"
          className="btn-secondary"
          aria-pressed={paused}
          title={paused ? 'Resume automatic refresh' : 'Pause automatic refresh'}
          onClick={() => setPaused(!paused)}
        >{paused ? <Play size={15} /> : <Pause size={15} />}{paused ? 'Paused' : 'Live'}</button>
        <Link className="btn-primary" to="/schedules">Create schedule</Link>
      </div>}
    />

    {retry.error && <div className="mb-4"><ErrorNotice error={retry.error} /></div>}

    <ReadinessStrip checks={data?.readiness ?? []} loading={overview.isLoading} />

    {isFresh ? <Onboarding data={data!} /> : <>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {overview.isLoading || !data
          ? Array.from({ length: 8 }).map((_, index) => <div className="card" key={index}><Skeleton className="h-3 w-24" /><Skeleton className="mt-3 h-7 w-16" /><Skeleton className="mt-2 h-3 w-20" /></div>)
          : kpisFor(data).map((kpi) => <KpiCard key={kpi.label} kpi={kpi} />)}
      </div>

      {data && <section className="card mt-6">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div><h2 className="font-semibold text-slate-900">Next 24 hours</h2><p className="muted">Every planned wave, sized by how many machines it acts on.</p></div>
          <Link className="link text-sm" to="/timeline">Open timeline</Link>
        </div>
        <div className="mt-4">{timeline.isLoading ? <Skeleton className="h-16 w-full" /> : <TimelineStrip blocks={timeline.data ?? []} />}</div>
      </section>}

      {/* Columns rather than a grid: these panels vary a lot in height (an empty "Live runs" beside
          a full "Next starts"), and grid rows are as tall as their tallest cell, which left a large
          void under the short one. Column flow packs them tightly. */}
      <div className="mt-6 xl:columns-2 xl:gap-6">
        <Panel title="Rollout plan" description="Each application's rings in the order they will start." action={<Link className="link text-sm" to="/schedules">Schedules</Link>}>
          {overview.isLoading || !data ? <TableSkeleton rows={4} columns={2} /> : <RolloutPlanPanel plans={data.rollout_plan} />}
        </Panel>

        <Panel title="Needs attention" description="Coverage gaps in both directions, and what keeps failing.">
          {overview.isLoading || !data ? <TableSkeleton rows={4} columns={2} /> : <AttentionPanel data={data} />}
        </Panel>

        <Panel title="Application health" description="One cell per wave in this window, newest on the right.">
          {overview.isLoading || !data ? <TableSkeleton rows={4} columns={2} /> : <HealthMatrix applications={data.applications} pinned={pinned} onTogglePin={togglePin} />}
        </Panel>

        {data && <Panel title="Reliability" description="How dependable the last waves were.">
          <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
            <Stat label="Wave success" value={percent(data.reliability.run_success_rate)} hint={`${data.reliability.runs_finished} finished`} />
            <Stat label="VM success" value={percent(data.reliability.vm_success_rate)} hint="machines reaching running" />
            <Stat label="Median start" value={seconds(data.reliability.median_seconds_to_running)} hint="request to running" />
            <Stat label="p95 start" value={seconds(data.reliability.p95_seconds_to_running)} hint="slowest tail" />
            <Stat label="Median lateness" value={seconds(data.reliability.median_lateness_seconds)} hint="vs planned start" />
            <Stat label="Worst lateness" value={seconds(data.reliability.worst_lateness_seconds)} hint="in this window" />
          </dl>
          {power && <div className="mt-4 border-t border-slate-100 pt-3">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Power state</p>
            {powerTotal === 0
              ? <p className="mt-1 text-sm text-slate-600">No virtual machine has been scanned yet. <Link className="link" to="/vms">Scan the inventory</Link> to see live state here.</p>
              : <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                {Object.entries(power.counts).map(([state, count]) => <Chip key={state} tone={state === 'running' ? 'success' : 'neutral'}>{count} {state}</Chip>)}
                {power.never_scanned > 0 && <Chip tone="warn">{power.never_scanned} never scanned</Chip>}
                {power.last_scan_at && <span className="text-xs text-slate-500">last scan {format(power.last_scan_at)}</span>}
              </div>}
          </div>}
        </Panel>}

        <Panel title="Live runs" description="Runs that have not finished yet." action={<Link className="link text-sm" to="/runs">All runs</Link>}>
          {runs.isLoading ? <TableSkeleton rows={3} columns={2} /> : runs.error ? <ErrorNotice error={runs.error} /> : live.length
            ? <ul className="space-y-2">{live.map((run) => <LiveRunRow key={run.id} run={run} />)}</ul>
            : <p className="py-8 text-center muted">Nothing is starting right now.</p>}
        </Panel>

        <Panel title="Next starts" description="Upcoming waves with a live countdown." action={<Link className="link text-sm" to="/timeline">Timeline</Link>}>
          {upcoming.isLoading ? <TableSkeleton rows={4} columns={2} /> : upcoming.error ? <ErrorNotice error={upcoming.error} /> : upcoming.data?.length
            ? <ul className="space-y-2">{upcoming.data.slice(0, 5).map((wave) => <UpcomingRow key={wave.schedule_id} wave={wave} />)}</ul>
            : <p className="py-8 text-center muted">No enabled schedule has a next start.</p>}
        </Panel>

        <Panel title="Recent runs" description="The last waves the scheduler or an operator triggered.">
          {runs.isLoading ? <TableSkeleton rows={4} columns={3} /> : recent.length
            ? <ul className="divide-y divide-slate-200">{recent.map((run) => <RecentRunRow key={run.id} run={run} canRetry={canRetry} retrying={retry.isPending} onRetry={retry.mutate} />)}</ul>
            : <p className="py-8 text-center muted">No run history yet. Trigger a schedule to create the first run.</p>}
        </Panel>

        <Panel title="Recent failures" description="Runs where at least one virtual machine did not start.">
          {runs.isLoading ? <TableSkeleton rows={4} columns={3} /> : failures.length
            ? <ul className="divide-y divide-slate-200">{failures.map((run) => <RecentRunRow key={run.id} run={run} canRetry={canRetry} retrying={retry.isPending} onRetry={retry.mutate} />)}</ul>
            : <p className="flex items-center justify-center gap-2 py-8 text-center muted"><Play size={15} aria-hidden="true" />No failed runs recorded.</p>}
        </Panel>
      </div>

      <section className="card mt-6">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div><h2 className="font-semibold text-slate-900">Activity</h2><p className="muted">Wave and per-machine events in this window.</p></div>
          <Link className="link text-sm" to="/runs">Full log</Link>
        </div>
        <ul className="mt-4 max-h-80 space-y-1.5 overflow-y-auto pr-1">
          {(activity.data?.events ?? []).slice(0, 30).map((event) => <li key={event.id} className="flex flex-wrap items-center gap-2 border-b border-slate-100 pb-1.5 text-sm last:border-0">
            <span className="w-36 shrink-0 text-xs text-slate-500">{format(event.at)}</span>
            <Chip tone={event.severity === 'error' ? 'danger' : event.severity === 'warning' ? 'warn' : event.severity === 'success' ? 'success' : 'accent'}>{event.kind}</Chip>
            {event.run_id ? <Link className="link" to={`/runs/${event.run_id}`}>{event.title}</Link> : <span className="font-medium text-slate-800">{event.title}</span>}
            <span className="min-w-0 flex-1 truncate text-xs text-slate-600">{event.summary}</span>
          </li>)}
          {(activity.data?.events ?? []).length === 0 && <li className="py-6 text-center muted">No activity in this window.</li>}
        </ul>
      </section>
    </>}

    {data && <p className="mt-4 text-center text-xs text-slate-500">
      Updated {format(data.generated_at)}{paused ? ' · auto-refresh paused' : ` · refreshing every ${POLL_MS / 1000}s`}
    </p>}
  </>
}
