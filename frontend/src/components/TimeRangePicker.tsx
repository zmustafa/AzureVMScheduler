import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { CalendarClock, ChevronDown } from 'lucide-react'
import {
  PRESETS, formatWindow, fromInputValue, offsetLabel, resolveRange, toInputValue,
  type PresetId, type RelativeUnit, type TimeRange,
} from '../lib/timeRange'

type Tab = 'presets' | 'relative' | 'range'

const TABS: { id: Tab; label: string }[] = [
  { id: 'presets', label: 'Presets' },
  { id: 'relative', label: 'Relative' },
  { id: 'range', label: 'Date range' },
]

/**
 * Window selector with quick presets, a rolling relative window, and an absolute range.
 * The Local/UTC switch only changes how this control reads and prints instants.
 */
export function TimeRangePicker({ value, onChange, now }: { value: TimeRange; onChange: (next: TimeRange) => void; now: number }) {
  const [open, setOpen] = useState(false)
  const [alignRight, setAlignRight] = useState(false)
  const [tab, setTab] = useState<Tab>(value.kind === 'absolute' ? 'range' : value.kind === 'relative' ? 'relative' : 'presets')
  const [zone, setZone] = useState<'local' | 'utc'>('local')
  const container = useRef<HTMLDivElement>(null)
  const labelId = useId()

  const resolved = useMemo(() => resolveRange(value, now), [value, now])
  const [amount, setAmount] = useState(value.kind === 'relative' ? String(value.amount) : '30')
  const [unit, setUnit] = useState<RelativeUnit>(value.kind === 'relative' ? value.unit : 'minutes')
  const [from, setFrom] = useState(() => toInputValue(resolved.from, 'local'))
  const [to, setTo] = useState(() => toInputValue(resolved.to, 'local'))

  useEffect(() => {
    if (!open) return
    const onClick = (event: MouseEvent) => { if (!container.current?.contains(event.target as Node)) setOpen(false) }
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onClick); document.removeEventListener('keydown', onKey) }
  }, [open])

  // Re-express the absolute inputs whenever the reading zone flips, so the instant is preserved.
  useEffect(() => {
    setFrom(toInputValue(resolved.from, zone))
    setTo(toInputValue(resolved.to, zone))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zone])

  const toggle = () => {
    const rect = container.current?.getBoundingClientRect()
    // 34rem panel: flip to the right edge rather than pushing the page sideways.
    if (rect) setAlignRight(rect.left + 544 > window.innerWidth - 16)
    setOpen(!open)
  }

  const choosePreset = (preset: PresetId) => { onChange({ kind: 'preset', preset }); setOpen(false) }

  const applyRelative = () => {
    const parsed = Number.parseInt(amount, 10)
    if (!Number.isFinite(parsed) || parsed < 1) return
    onChange({ kind: 'relative', amount: parsed, unit })
    setOpen(false)
  }

  const fromMs = fromInputValue(from, zone)
  const toMs = fromInputValue(to, zone)
  const rangeInvalid = !Number.isFinite(fromMs) || !Number.isFinite(toMs) || toMs <= fromMs

  const applyRange = () => {
    if (rangeInvalid) return
    onChange({ kind: 'absolute', from: fromMs, to: toMs })
    setOpen(false)
  }

  return <div className="relative" ref={container}>
    <button
      type="button"
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-label={`Time range: ${resolved.label}`}
      onClick={toggle}
      className="flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm transition hover:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure"
    >
      <CalendarClock size={15} className="shrink-0 text-slate-500" />
      <span className="font-medium">{resolved.label}</span>
      <span className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[11px] text-slate-600">{offsetLabel(zone, now)}</span>
      <ChevronDown size={15} className="text-slate-500" />
    </button>

    {open && <div role="dialog" aria-labelledby={labelId} className={`absolute z-40 mt-1 w-[34rem] max-w-[calc(100vw-2rem)] rounded-lg border border-slate-200 bg-white shadow-xl ${alignRight ? 'right-0' : 'left-0'}`}>
      <h2 id={labelId} className="sr-only">Choose a time range</h2>
      <div className="flex">
        <div className="w-32 shrink-0 border-r border-slate-200 py-2">
          {TABS.map((item) => <button
            key={item.id}
            type="button"
            onClick={() => setTab(item.id)}
            aria-current={tab === item.id}
            className={`block w-full px-3 py-2 text-left text-sm transition ${tab === item.id ? 'bg-blue-50 font-semibold text-blue-800' : 'text-slate-700 hover:bg-slate-50'}`}
          >{item.label}</button>)}
        </div>

        <div className="min-w-0 flex-1 p-3">
          <div className="mb-3 flex items-center justify-between gap-3">
            <p className="text-xs text-slate-500">Times shown in {zone === 'utc' ? 'UTC' : `local (${offsetLabel('local', now)})`}</p>
            <div className="flex rounded-md border border-slate-300 p-0.5" role="group" aria-label="Interpret times as">
              {(['local', 'utc'] as const).map((item) => <button
                key={item}
                type="button"
                aria-pressed={zone === item}
                onClick={() => setZone(item)}
                className={`rounded px-2 py-0.5 text-xs transition ${zone === item ? 'bg-blue-600 text-white' : 'text-slate-600 hover:bg-slate-100'}`}
              >{item === 'local' ? 'Local' : 'UTC'}</button>)}
            </div>
          </div>

          {tab === 'presets' && <ul className="grid grid-cols-2 gap-x-4 gap-y-0.5">
            {PRESETS.map((preset) => <li key={preset.id}>
              <button
                type="button"
                onClick={() => choosePreset(preset.id)}
                aria-current={value.kind === 'preset' && value.preset === preset.id}
                className={`w-full rounded px-2 py-1.5 text-left text-sm transition hover:bg-blue-50 ${value.kind === 'preset' && value.preset === preset.id ? 'font-semibold text-blue-800' : 'text-blue-700'}`}
              >{preset.label}</button>
            </li>)}
          </ul>}

          {tab === 'relative' && <div className="space-y-3">
            <p className="text-sm text-slate-600">A rolling window that always ends at the current time.</p>
            <div className="flex items-end gap-2">
              <label className="text-sm text-slate-700">Last
                <input className="ml-2 !w-24" type="number" autoComplete="off" min={1} value={amount} onChange={(event) => setAmount(event.target.value)} aria-label="Relative window amount" />
              </label>
              <select className="!w-auto" value={unit} onChange={(event) => setUnit(event.target.value as RelativeUnit)} aria-label="Relative window unit">
                <option value="minutes">minutes</option><option value="hours">hours</option><option value="days">days</option>
              </select>
              <button type="button" className="btn-primary !py-1.5" onClick={applyRelative}>Apply</button>
            </div>
          </div>}

          {tab === 'range' && <div className="space-y-3">
            <div className="flex flex-wrap gap-3">
              <label className="text-sm text-slate-700">From
                <input className="mt-1" type="datetime-local" autoComplete="off" value={from} onChange={(event) => setFrom(event.target.value)} aria-label="Range start" />
              </label>
              <label className="text-sm text-slate-700">To
                <input className="mt-1" type="datetime-local" autoComplete="off" value={to} onChange={(event) => setTo(event.target.value)} aria-label="Range end" />
              </label>
            </div>
            {rangeInvalid
              ? <p className="text-xs text-rose-700">Pick a start and an end, with the end after the start.</p>
              : <p className="text-xs text-slate-500">{formatWindow(fromMs, toMs, zone)}</p>}
            <button type="button" className="btn-primary !py-1.5" disabled={rangeInvalid} onClick={applyRange}>Apply range</button>
          </div>}
        </div>
      </div>
    </div>}
  </div>
}
