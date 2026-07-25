import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { CalendarRange, ChevronLeft, ChevronRight, ListTree } from 'lucide-react'
import { api } from '../api'
import { useScheduleIndex } from '../lib/queries'
import { actionMeta } from '../lib/actions'
import { ActionBadge } from '../components/ActionBits'
import { countdownText, serverNow, useDisplayTimezone, useTick, zoneLabel } from '../lib/time'
import { EmptyState, ErrorNotice, PageHeader, TableSkeleton } from '../components/Ui'
import type { TimelineBlock } from '../types'

const HOUR_MS = 3_600_000
const DAY_MS = 24 * HOUR_MS

type Span = 'day' | 'week'

/** Wall-clock parts of an instant in a specific IANA zone. */
function zoneParts(zone: string, ms: number) {
  const parts = new Intl.DateTimeFormat('en-US', { timeZone: zone, hour12: false, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).formatToParts(new Date(ms))
  const value = (type: string) => Number(parts.find((part) => part.type === type)?.value ?? 0)
  return { year: value('year'), month: value('month'), day: value('day'), hour: value('hour') % 24, minute: value('minute'), second: value('second') }
}

function zoneOffsetMs(zone: string, ms: number): number {
  const parts = zoneParts(zone, ms)
  return Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second) - Math.floor(ms / 1000) * 1000
}

/** Instant of local midnight in `zone`, offset by whole days. Two passes settle DST boundaries. */
function startOfDayInZone(ms: number, zone: string, dayOffset = 0): number {
  const parts = zoneParts(zone, ms)
  const wall = Date.UTC(parts.year, parts.month - 1, parts.day + dayOffset, 0, 0, 0)
  let instant = wall - zoneOffsetMs(zone, ms)
  instant = wall - zoneOffsetMs(zone, instant)
  return instant
}

function hourInZone(ms: number, zone: string): number {
  return zoneParts(zone, ms).hour
}

function dayLabel(ms: number, zone: string): string {
  try {
    return new Intl.DateTimeFormat('en-GB', { timeZone: zone, weekday: 'short', day: 'numeric', month: 'short' }).format(new Date(ms))
  } catch {
    return new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }).format(new Date(ms))
  }
}

function BlockTip({ block, zone, axisZone }: { block: TimelineBlock; zone?: string; axisZone: string }) {
  const { format } = useDisplayTimezone()
  return <div className="pointer-events-none absolute bottom-full left-0 z-20 mb-1 hidden w-64 rounded-lg border border-slate-200 bg-white p-2.5 text-left shadow-xl group-hover:block group-focus-visible:block">
    <p className="text-sm font-semibold text-slate-900">{block.name}</p>
    <p className="mt-1"><ActionBadge action={block.action} stopMode={block.stop_mode} size="sm" /></p>
    <p className="mt-0.5 text-xs text-slate-600">{block.group_path || 'No group resolved'}</p>
    <p className="mt-1.5 text-xs text-slate-800">{format(block.start, zone ?? axisZone)}</p>
    <p className="text-xs font-semibold text-blue-700">{countdownText(block.start, serverNow(), zone ?? axisZone)}</p>
    <p className="mt-1 text-xs text-slate-600">{block.vm_count} VM{block.vm_count === 1 ? '' : 's'} · {block.stagger_seconds}s stagger</p>
  </div>
}

/** Horizontal band of upcoming start waves, grouped by application / ring path. */
export function TimelinePage() {
  const { resolve } = useDisplayTimezone()
  const axisZone = resolve(null)
  const [span, setSpan] = useState<Span>('day')
  const [dayOffset, setDayOffset] = useState(0)
  useTick(30_000)

  const windowStart = useMemo(() => startOfDayInZone(serverNow(), axisZone, dayOffset), [axisZone, dayOffset])
  const windowEnd = windowStart + (span === 'day' ? DAY_MS : 7 * DAY_MS)

  const path = `/timeline?from=${encodeURIComponent(new Date(windowStart).toISOString())}&to=${encodeURIComponent(new Date(windowEnd).toISOString())}`
  const list = useQuery({ queryKey: ['timeline', path], queryFn: () => api<TimelineBlock[]>(path), refetchInterval: 60_000 })
  const scheduleIndex = useScheduleIndex()
  const zoneFor = useMemo(() => {
    const zones = new Map((scheduleIndex.data?.items ?? []).map((item) => [item.id, item.timezone]))
    return (block: TimelineBlock) => zones.get(block.schedule_id)
  }, [scheduleIndex.data])

  const rows = useMemo(() => {
    const grouped = new Map<string, TimelineBlock[]>()
    for (const block of list.data ?? []) {
      const key = block.group_path || 'Unassigned'
      grouped.set(key, [...(grouped.get(key) ?? []), block])
    }
    return [...grouped.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [list.data])

  const ticks = useMemo(() => {
    if (span === 'day') return Array.from({ length: 12 }, (_, index) => ({ at: windowStart + index * 2 * HOUR_MS, label: `${String(hourInZone(windowStart + index * 2 * HOUR_MS, axisZone)).padStart(2, '0')}:00` }))
    return Array.from({ length: 7 }, (_, index) => {
      const at = startOfDayInZone(windowStart, axisZone, index)
      return { at, label: dayLabel(at, axisZone) }
    })
  }, [span, windowStart, axisZone])

  const total = windowEnd - windowStart
  const positionAt = (ms: number) => ((ms - windowStart) / total) * 100
  const position = (iso: string) => positionAt(new Date(iso).getTime())
  const now = serverNow()
  const nowPercent = positionAt(now)

  return <>
    <PageHeader
      title="Timeline"
      description={`Start waves plotted in ${axisZone} (${zoneLabel(axisZone)}). Switch the display timezone in the header to re-plot.`}
      action={<div className="flex items-center gap-2">
        <div className="inline-flex rounded-lg border border-slate-300 bg-white p-0.5" role="group" aria-label="Timeline span">
          {(['day', 'week'] as Span[]).map((item) => <button
            key={item}
            type="button"
            aria-pressed={span === item}
            onClick={() => { setSpan(item); setDayOffset(0) }}
            className={`rounded-md px-2.5 py-1 text-xs font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure ${span === item ? 'bg-blue-100 text-blue-800' : 'text-slate-600 hover:bg-slate-50'}`}
          >{item === 'day' ? '24 hours' : '7 days'}</button>)}
        </div>
        <button type="button" className="btn-secondary !px-2" aria-label="Previous window" onClick={() => setDayOffset((value) => value - (span === 'day' ? 1 : 7))}><ChevronLeft size={16} /></button>
        <button type="button" className="btn-secondary !py-1" onClick={() => setDayOffset(0)}>Today</button>
        <button type="button" className="btn-secondary !px-2" aria-label="Next window" onClick={() => setDayOffset((value) => value + (span === 'day' ? 1 : 7))}><ChevronRight size={16} /></button>
      </div>}
    />

    <p className="mb-3 flex items-center gap-2 text-sm text-slate-600"><CalendarRange size={16} aria-hidden="true" />{dayLabel(windowStart, axisZone)}{span === 'week' ? ` — ${dayLabel(windowEnd - DAY_MS, axisZone)}` : ''}</p>

    {list.error && <div className="mb-4"><ErrorNotice error={list.error} /></div>}

    {list.isLoading ? <TableSkeleton rows={5} columns={3} /> : rows.length === 0 ? <EmptyState
      icon={<ListTree size={22} />}
      title="No start waves in this window"
      description="Enable a schedule, or move to another day, to see its waves plotted here."
    /> : <div className="surface overflow-x-auto">
      <div className="min-w-[52rem]">
        <div className="flex border-b border-slate-200 bg-slate-50">
          <div className="sticky left-0 z-10 w-56 shrink-0 border-r border-slate-200 bg-slate-50 px-3 py-2 text-xs font-semibold uppercase tracking-wider text-slate-600">Application / ring</div>
          <div className="relative flex-1">
            {ticks.map((tick) => <span key={tick.at} className="absolute top-0 py-2 text-[11px] text-slate-500" style={{ left: `${positionAt(tick.at)}%` }}>{tick.label}</span>)}
            <div className="h-8" />
          </div>
        </div>

        {rows.map(([groupPath, blocks]) => <div key={groupPath} className="flex border-b border-slate-200 last:border-b-0">
          <div className="sticky left-0 z-10 w-56 shrink-0 border-r border-slate-200 bg-white px-3 py-3 text-sm text-slate-800">
            <span className="block truncate" title={groupPath}>{groupPath}</span>
            <span className="text-xs text-slate-500">{blocks.length} wave{blocks.length === 1 ? '' : 's'}</span>
          </div>
          <div className="relative min-h-[3.5rem] flex-1">
            {ticks.map((tick) => <span key={tick.at} className="absolute inset-y-0 w-px bg-slate-100" style={{ left: `${positionAt(tick.at)}%` }} aria-hidden="true" />)}
            {nowPercent >= 0 && nowPercent <= 100 && <span className="absolute inset-y-0 z-10 w-0.5 bg-rose-500" style={{ left: `${nowPercent}%` }} aria-hidden="true" />}
            {blocks.map((block) => {
              const left = Math.max(position(block.start), 0)
              const width = Math.max(position(block.end) - left, 1.5)
              const zone = zoneFor(block)
              const meta = actionMeta(block.action)
              const Icon = meta.icon
              return <Link
                key={`${block.schedule_id}-${block.start}`}
                to={`/schedules/${block.schedule_id}`}
                title={`${meta.label}: ${block.name} — ${block.vm_count} VM${block.vm_count === 1 ? '' : 's'}`}
                className={`group absolute top-3 flex h-8 items-center gap-1 overflow-visible rounded-md border px-2 text-[11px] font-medium transition hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure ${meta.chip}`}
                style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%`, minWidth: '4.5rem' }}
              >
                <Icon size={11} className="shrink-0" aria-hidden="true" />
                <span className="truncate">{block.name} · {block.vm_count} VM{block.vm_count === 1 ? '' : 's'}</span>
                <BlockTip block={block} zone={zone} axisZone={axisZone} />
              </Link>
            })}
          </div>
        </div>)}
      </div>
    </div>}

    <p className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-slate-500">
      <span className="inline-flex items-center gap-2"><span className="inline-block h-3 w-0.5 bg-rose-500" aria-hidden="true" />Current time</span>
      {(['start', 'stop'] as const).map((option) => {
        const meta = actionMeta(option)
        return <span key={option} className="inline-flex items-center gap-1.5"><span className={`inline-block h-3 w-3 rounded-sm border ${meta.chip}`} aria-hidden="true" />{meta.label} wave</span>
      })}
      <span>Block width reflects the stagger spread across the wave.</span>
    </p>
  </>
}

