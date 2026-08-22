import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'
import { Activity } from 'lucide-react'
import { api } from '../api'
import { useDisplayTimezone, useTick, serverNow } from '../lib/time'
import { useScheduleIndex } from '../lib/queries'
import { DEFAULT_RANGE, resolveRange, type TimeRange } from '../lib/timeRange'
import { useSort } from '../lib/sorting'
import { StatusBadge } from '../components/StatusBadge'
import { SortHeader } from '../components/SortHeader'
import { TimeRangePicker } from '../components/TimeRangePicker'
import { ActivityTimeline, clampSelection, type Selection } from '../components/ActivityTimeline'
import { RunProgress, countsText, isRunActive, latenessText, runDurationText } from '../components/RunProgress'
import { Chip, EmptyState, ErrorNotice, Pagination, PageHeader, SearchInput, TableSkeleton } from '../components/Ui'
import type { ActivityResponse, Paged, ScheduleRun } from '../types'

const LIMIT = 50
const RUN_STATUSES = ['pending', 'running', 'succeeded', 'partially_failed', 'failed', 'timed_out', 'cancelled', 'skipped']

/** Human label for the actor that produced the run. */
function triggeredBy(run: ScheduleRun): string {
  if (run.trigger === 'scheduler') return 'Scheduler'
  return run.triggered_by ? `Manual · ${run.triggered_by}` : 'Manual'
}

function useZoneFor(): (run: ScheduleRun) => string | undefined {
  const index = useScheduleIndex()
  return useMemo(() => {
    const zones = new Map<string, string>((index.data?.items ?? []).map((item) => [item.id, item.timezone]))
    return (run: ScheduleRun) => (run.schedule_id ? zones.get(run.schedule_id) : undefined)
  }, [index.data])
}

function TimingCell({ run, zone }: { run: ScheduleRun; zone?: string }) {
  const { format } = useDisplayTimezone()
  const lateness = latenessText(run)
  return <div className="min-w-[13rem]">
    <span className="block text-slate-800">{format(run.started_at ?? run.scheduled_for ?? run.created_at, zone)}</span>
    <span className="text-xs text-slate-500">Planned {format(run.scheduled_for, zone)}</span>
    {lateness && <span className={`ml-1 text-xs font-semibold ${lateness.endsWith('late') ? 'text-amber-800' : 'text-slate-600'}`}>· {lateness}</span>}
  </div>
}

function RunCard({ run, zone }: { run: ScheduleRun; zone?: string }) {
  const { format } = useDisplayTimezone()
  return <li className="border-t border-slate-200 p-4 first:border-t-0">
    <div className="flex flex-wrap items-start justify-between gap-2">
      <Link className="link" to={`/runs/${run.id}`}>{run.schedule_name}</Link>
      <div className="flex items-center gap-2"><StatusBadge value={run.mode} /><StatusBadge value={run.status} /></div>
    </div>
    <p className="mt-1 text-xs text-slate-500">{format(run.started_at ?? run.scheduled_for ?? run.created_at, zone)} · {runDurationText(run)} · {triggeredBy(run)}</p>
    <div className="mt-2"><RunProgress run={run} size="sm" /></div>
    <p className="mt-1.5 text-xs text-slate-600">{countsText(run)}</p>
  </li>
}

/** Paged run history over a chosen time window, with an activity timeline and detailed log. */
export function RunsPage() {
  const [params, setParams] = useSearchParams()
  const [status, setStatus] = useState(params.get('status') ?? '')
  const [trigger, setTrigger] = useState(params.get('trigger') ?? '')
  const [mode, setMode] = useState(params.get('mode') ?? '')
  const [query, setQuery] = useState('')
  const [range, setRange] = useState<TimeRange>(DEFAULT_RANGE)
  const [selection, setSelection] = useState<Selection | null>(null)
  const [offset, setOffset] = useState(0)
  const zoneFor = useZoneFor()
  const { sort, toggle, params: sortParams } = useSort({ key: 'created_at', direction: 'desc' }, ['created_at', 'scheduled_for', 'started_at'])

  // Rolling windows follow the clock, but only in 30s steps so query keys stay stable.
  useTick(30_000)
  const anchor = Math.floor(serverNow() / 30_000) * 30_000
  const window = useMemo(() => resolveRange(range, anchor), [range, anchor])

  // The brush holds absolute instants, so a sliding window just clamps it rather than dropping it.
  const active = selection ? clampSelection(selection, window.from, window.to) : { from: window.from, to: window.to }

  useEffect(() => { setOffset(0) }, [status, trigger, mode, query, window.from, window.to, active.from, active.to, sort])
  useEffect(() => {
    const next = new URLSearchParams()
    if (status) next.set('status', status)
    if (trigger) next.set('trigger', trigger)
    if (mode) next.set('mode', mode)
    setParams(next, { replace: true })
  }, [status, trigger, mode, setParams])

  const path = useMemo(() => {
    const search = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) })
    search.set('from', new Date(window.from).toISOString())
    search.set('to', new Date(window.to).toISOString())
    if (status) search.set('status', status)
    if (trigger) search.set('trigger', trigger)
    return `/runs?${search.toString()}&${sortParams}`
  }, [status, trigger, offset, window.from, window.to, sortParams])

  const list = useQuery({ queryKey: ['runs', path], queryFn: () => api<Paged<ScheduleRun>>(path), refetchInterval: (query) => ((query.state.data?.items ?? []).some(isRunActive) ? 15_000 : 60_000), placeholderData: (previous) => previous })

  const activityPath = useMemo(() => {
    const search = new URLSearchParams({ limit: '500' })
    search.set('from', new Date(window.from).toISOString())
    search.set('to', new Date(window.to).toISOString())
    return `/runs/activity?${search.toString()}`
  }, [window.from, window.to])

  const activity = useQuery({ queryKey: ['runs-activity', activityPath], queryFn: () => api<ActivityResponse>(activityPath), refetchInterval: 20_000, placeholderData: (previous) => previous })

  const events = useMemo(() => (activity.data?.events ?? []).filter((event) => {
    const stamp = new Date(event.at).getTime()
    return stamp >= active.from && stamp <= active.to
  }), [activity.data, active.from, active.to])

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return (list.data?.items ?? []).filter((run) => {
      if (mode && run.mode !== mode) return false
      if (needle && !run.schedule_name.toLowerCase().includes(needle)) return false
      const stamp = new Date(run.started_at ?? run.scheduled_for ?? run.created_at).getTime()
      return stamp >= active.from && stamp <= active.to
    })
  }, [list.data, mode, query, active.from, active.to])

  const zoom = useCallback((preset: TimeRange) => {
    setSelection(null)
    setRange(preset)
  }, [])

  const narrowed = active.from !== window.from || active.to !== window.to
  const filtered = !!(mode || query.trim() || narrowed)

  return <>
    <PageHeader title="Runs" description="Every scheduled and manual start wave, with per-VM outcomes." />

    <div className="mb-4 flex flex-wrap items-center gap-3">
      <SearchInput value={query} onChange={setQuery} placeholder="Search by schedule" label="Search runs by schedule name" />
      <TimeRangePicker value={range} onChange={(next) => { setSelection(null); setRange(next) }} now={anchor} />
      <select className="!w-auto" value={status} onChange={(event) => setStatus(event.target.value)} aria-label="Filter by run status">
        <option value="">Any status</option>
        {RUN_STATUSES.map((item) => <option key={item} value={item}>{item.replaceAll('_', ' ')}</option>)}
      </select>
      <select className="!w-auto" value={mode} onChange={(event) => setMode(event.target.value)} aria-label="Filter by execution mode">
        <option value="">Any mode</option><option value="pending">Pending</option><option value="mock">Mock</option><option value="real">Real</option>
      </select>
      <select className="!w-auto" value={trigger} onChange={(event) => setTrigger(event.target.value)} aria-label="Filter by trigger">
        <option value="">Any trigger</option><option value="scheduler">Scheduler</option><option value="manual">Manual</option>
      </select>
    </div>

    {activity.error && <div className="mb-4"><ErrorNotice error={activity.error} /></div>}

    <ActivityTimeline
      events={events}
      from={window.from}
      to={window.to}
      selection={active}
      onSelectionChange={setSelection}
      onZoom={zoom}
      shown={events.length}
      narrowed={narrowed}
      onReset={() => setSelection(null)}
      loading={activity.isLoading}
    />

    {list.error && <div className="mb-4"><ErrorNotice error={list.error} /></div>}

    {list.isLoading ? <TableSkeleton columns={7} /> : rows.length === 0 ? <EmptyState
      icon={<Activity size={22} />}
      title="No runs match this view"
      description={filtered || status || trigger ? 'Widen the time range, drag the window handles back out, or relax the filters to see more of the run history.' : 'Runs appear here as soon as the scheduler starts a wave or an operator triggers one manually.'}
    /> : <>
      <ul className="surface md:hidden">{rows.map((run) => <RunCard key={run.id} run={run} zone={zoneFor(run)} />)}</ul>

      <div className="surface hidden md:block">
        <div className="max-h-[70vh] overflow-auto">
          <table className="u-table">
            <thead><tr>
              <SortHeader label="Schedule" sortKey="schedule_name" sort={sort} onSort={toggle} />
              <SortHeader label="Started / planned" sortKey="started_at" sort={sort} onSort={toggle} />
              <SortHeader label="Duration" />
              <SortHeader label="Progress" />
              <SortHeader label="Status" sortKey="status" sort={sort} onSort={toggle} />
              <SortHeader label="Mode" sortKey="mode" sort={sort} onSort={toggle} />
              <SortHeader label="Trigger" sortKey="trigger" sort={sort} onSort={toggle} />
            </tr></thead>
            <tbody>{rows.map((run) => <tr key={run.id}>
              <td>
                <Link className="link" to={`/runs/${run.id}`}>{run.schedule_name}</Link>
                <span className="block truncate text-xs text-slate-500">{run.connection_name ? `${run.connection_name}${run.connection_tenant_id ? ` (${run.connection_tenant_id})` : ''}` : 'Tenant resolved per VM'}</span>
              </td>
              <td><TimingCell run={run} zone={zoneFor(run)} /></td>
              <td className="tabular-nums">{runDurationText(run)}</td>
              <td className="min-w-[11rem]"><RunProgress run={run} size="sm" /><span className="mt-1 block text-xs text-slate-600">{countsText(run)}</span></td>
              <td><StatusBadge value={run.status} /></td>
              <td><Chip tone={run.mode === 'real' ? 'warn' : 'accent'}>{run.mode}</Chip></td>
              <td className="text-xs text-slate-600">{triggeredBy(run)}</td>
            </tr>)}</tbody>
          </table>
        </div>
        <Pagination total={list.data?.total ?? 0} limit={LIMIT} offset={offset} onChange={setOffset} />
      </div>
    </>}
  </>
}

