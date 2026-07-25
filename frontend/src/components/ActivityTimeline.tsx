import { useCallback, useMemo, useRef } from 'react'
import { Link } from 'react-router'
import { BarChart3 } from 'lucide-react'
import { bucketSize, formatTick, formatWindow, type TimeRange } from '../lib/timeRange'
import { Chip } from './Ui'
import type { ActivityEvent } from '../types'

export type Selection = { from: number; to: number }

const ZOOMS: [string, TimeRange][] = [
  ['1h', { kind: 'preset', preset: 'last60m' }],
  ['6h', { kind: 'relative', amount: 6, unit: 'hours' }],
  ['24h', { kind: 'preset', preset: 'last24h' }],
  ['7d', { kind: 'preset', preset: 'last7d' }],
  ['All', { kind: 'preset', preset: 'last30d' }],
]

/** One source of truth for severity: rank, bar colour and legend label cannot drift apart. */
const SEVERITIES = [
  { key: 'info', label: 'Info', tone: 'bg-blue-400' },
  { key: 'success', label: 'Success', tone: 'bg-emerald-400' },
  { key: 'warning', label: 'Warning', tone: 'bg-amber-400' },
  { key: 'error', label: 'Error', tone: 'bg-rose-500' },
] as const

const SEVERITY_RANK: Record<string, number> = Object.fromEntries(SEVERITIES.map((item, rank) => [item.key, rank]))
const BAR_TONES: string[] = SEVERITIES.map((item) => item.tone)
const CHIP_TONES = { info: 'accent', success: 'success', warning: 'warn', error: 'danger' } as const

function severityTone(severity: string) {
  return CHIP_TONES[severity as keyof typeof CHIP_TONES] ?? 'neutral'
}

type Bucket = { start: number; end: number; count: number; worst: number }

function buildBuckets(events: ActivityEvent[], from: number, to: number): Bucket[] {
  const size = bucketSize(Math.max(to - from, 1))
  const count = Math.max(Math.ceil((to - from) / size), 1)
  const buckets: Bucket[] = Array.from({ length: count }, (_, index) => ({
    start: from + index * size,
    end: from + (index + 1) * size,
    count: 0,
    worst: 0,
  }))
  for (const event of events) {
    const stamp = new Date(event.at).getTime()
    if (!Number.isFinite(stamp) || stamp < from || stamp > to) continue
    const bucket = buckets[Math.min(Math.floor((stamp - from) / size), count - 1)]
    bucket.count += 1
    bucket.worst = Math.max(bucket.worst, SEVERITY_RANK[event.severity] ?? 0)
  }
  return buckets
}

/** Two-handle brush over the histogram. Handles are real sliders so the window is keyboard-reachable. */
function Brush({ from, to, selection, onChange }: { from: number; to: number; selection: Selection; onChange: (next: Selection) => void }) {
  const track = useRef<HTMLDivElement>(null)
  const span = Math.max(to - from, 1)
  const startPct = ((selection.from - from) / span) * 100
  const endPct = ((selection.to - from) / span) * 100

  const drag = useCallback((edge: 'from' | 'to') => (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    const element = track.current
    if (!element) return
    const move = (point: PointerEvent) => {
      const rect = element.getBoundingClientRect()
      const ratio = Math.min(Math.max((point.clientX - rect.left) / rect.width, 0), 1)
      const at = from + ratio * span
      onChange(edge === 'from' ? { from: Math.min(at, selection.to - 1000), to: selection.to } : { from: selection.from, to: Math.max(at, selection.from + 1000) })
    }
    const stop = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', stop) }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop)
  }, [from, span, selection, onChange])

  const nudge = (edge: 'from' | 'to') => (event: React.KeyboardEvent<HTMLDivElement>) => {
    const step = span / 40
    const delta = event.key === 'ArrowLeft' ? -step : event.key === 'ArrowRight' ? step : 0
    if (!delta) return
    event.preventDefault()
    if (edge === 'from') onChange({ from: Math.min(Math.max(selection.from + delta, from), selection.to - 1000), to: selection.to })
    else onChange({ from: selection.from, to: Math.max(Math.min(selection.to + delta, to), selection.from + 1000) })
  }

  const handle = (edge: 'from' | 'to', pct: number, value: number) => <div
    key={edge}
    role="slider"
    tabIndex={0}
    aria-label={edge === 'from' ? 'Window start' : 'Window end'}
    aria-valuemin={from}
    aria-valuemax={to}
    aria-valuenow={value}
    aria-valuetext={new Date(value).toLocaleString()}
    onPointerDown={drag(edge)}
    onKeyDown={nudge(edge)}
    className="absolute top-1/2 h-4 w-4 -translate-x-1/2 -translate-y-1/2 cursor-ew-resize rounded-full border-2 border-white bg-indigo-600 shadow focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure"
    style={{ left: `${pct}%` }}
  />

  return <div ref={track} className="relative mt-1 h-4 select-none">
    <div className="absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 rounded bg-slate-200" />
    <div className="absolute top-1/2 h-1 -translate-y-1/2 rounded bg-blue-600" style={{ left: `${startPct}%`, width: `${Math.max(endPct - startPct, 0)}%` }} />
    {handle('from', startPct, selection.from)}
    {handle('to', endPct, selection.to)}
  </div>
}

export function ActivityTimeline({
  events, from, to, selection, onSelectionChange, onZoom, shown, narrowed, onReset, loading,
}: {
  events: ActivityEvent[]
  from: number
  to: number
  selection: Selection
  onSelectionChange: (next: Selection) => void
  onZoom: (range: TimeRange) => void
  shown: number
  narrowed: boolean
  onReset: () => void
  loading?: boolean
}) {
  const buckets = useMemo(() => buildBuckets(events, from, to), [events, from, to])
  const peak = Math.max(...buckets.map((bucket) => bucket.count), 1)
  const span = Math.max(to - from, 1)

  return <section className="surface mb-4 p-4" aria-label="Run activity timeline">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="flex flex-wrap items-center gap-2 text-sm text-slate-700">
        <BarChart3 size={15} className="text-slate-500" aria-hidden="true" />
        <span className="font-semibold">Activity window</span>
        <span className="text-slate-600">{formatWindow(selection.from, selection.to, 'local')}</span>
        <Chip tone="accent">{shown} shown</Chip>
      </p>
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={onReset}
          disabled={!narrowed}
          className="mr-1 rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 transition hover:bg-slate-50 disabled:invisible"
        >Reset window</button>
        <span className="flex gap-1" role="group" aria-label="Zoom the window">
          {ZOOMS.map(([label, range]) => <button
            key={label}
            type="button"
            onClick={() => onZoom(range)}
            className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 transition hover:bg-slate-50"
          >{label}</button>)}
        </span>
      </div>
    </div>

    <div className="mt-3 rounded bg-slate-50 p-2">
      <div className="flex h-16 items-end gap-px" aria-hidden="true">
        {buckets.map((bucket) => {
          const inside = bucket.end > selection.from && bucket.start < selection.to
          return <div key={bucket.start} className="flex h-full flex-1 items-end" title={`${bucket.count} event${bucket.count === 1 ? '' : 's'} · ${formatTick(bucket.start, span, 'local')}`}>
            {bucket.count > 0 && <div
              className={`w-full rounded-sm transition-opacity ${BAR_TONES[bucket.worst]} ${inside ? '' : 'opacity-25'}`}
              style={{ height: `${Math.max((bucket.count / peak) * 100, 8)}%` }}
            />}
          </div>
        })}
      </div>
      <Brush from={from} to={to} selection={selection} onChange={onSelectionChange} />
      <div className="mt-1 flex justify-between text-[11px] text-slate-500">
        <span>{formatTick(from, span, 'local')}</span>
        <span>{formatTick(from + span / 2, span, 'local')}</span>
        <span>{formatTick(to, span, 'local')}</span>
      </div>
    </div>

    {/* The colour is the worst thing that happened in a slice, not a count — say so, or a single
        red bar among green ones reads as "mostly failing" rather than "one failure here". */}
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
      <span>Bar colour shows the worst event in that slice; height is how many:</span>
      {SEVERITIES.map((item) => <span key={item.key} className="inline-flex items-center gap-1">
        <span className={`h-2.5 w-2.5 rounded-sm ${item.tone}`} aria-hidden="true" />{item.label}
      </span>)}
      <span className="inline-flex items-center gap-1">
        <span className="h-2.5 w-2.5 rounded-sm bg-slate-400 opacity-25" aria-hidden="true" />Outside the selected window
      </span>
    </div>

    {loading
      ? <p className="mt-3 text-sm text-slate-500">Loading activity…</p>
      : events.length === 0
        ? <p className="mt-3 text-sm text-slate-500">No wave or start-attempt activity in this window.</p>
        : <ul className="mt-3 max-h-96 space-y-2 overflow-y-auto pr-1">
          {events.map((event) => <li key={event.id} className="flex gap-3 rounded-lg border border-slate-200 p-3">
            <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${BAR_TONES[SEVERITY_RANK[event.severity] ?? 0]}`} aria-hidden="true" />
            <div className="min-w-0">
              <p className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-xs text-slate-500">{new Date(event.at).toLocaleString()}</span>
                <Chip tone={severityTone(event.severity)}>{event.severity}</Chip>
                <Chip>{event.kind}</Chip>
                {event.run_id
                  ? <Link className="link font-medium" to={`/runs/${event.run_id}`}>{event.title}</Link>
                  : <span className="font-medium text-slate-800">{event.title}</span>}
                {event.schedule_name && <span className="text-xs text-slate-500">{event.schedule_name}</span>}
              </p>
              <p className="mt-0.5 break-words text-sm text-slate-700">{event.summary}</p>
            </div>
          </li>)}
        </ul>}
  </section>
}

/** Clamp a selection back inside its window; used whenever the outer range changes. */
export function clampSelection(selection: Selection, from: number, to: number): Selection {
  const start = Math.min(Math.max(selection.from, from), to)
  const end = Math.min(Math.max(selection.to, start), to)
  return { from: start, to: end }
}
