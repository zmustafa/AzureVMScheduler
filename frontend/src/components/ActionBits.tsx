import { ShieldCheck } from 'lucide-react'

import { actionMeta, RING_ORDER_LABEL, STOP_MODE_HELP, STOP_MODE_LABEL } from '../lib/actions'
import type { RingOrder, ScheduleAction, StopMode } from '../types'

/** Colour plus icon plus word — a wave's action is never signalled by colour alone. */
export function ActionBadge({ action, stopMode, size = 'md' }: { action: ScheduleAction; stopMode?: StopMode; size?: 'sm' | 'md' }) {
  const meta = actionMeta(action)
  const Icon = meta.icon
  const detail = action === 'stop' && stopMode ? ` · ${STOP_MODE_LABEL[stopMode]}` : ''
  return <span
    title={action === 'stop' && stopMode ? STOP_MODE_HELP[stopMode] : undefined}
    className={`inline-flex items-center gap-1 rounded-full border font-medium ${meta.chip} ${size === 'sm' ? 'px-1.5 py-0 text-[11px]' : 'px-2 py-0.5 text-xs'}`}
  >
    <Icon size={size === 'sm' ? 10 : 12} aria-hidden="true" />
    {meta.label}{detail}
  </span>
}

/** Shown wherever a machine could otherwise look stoppable. */
export function ProtectedChip({ inherited }: { inherited?: boolean }) {
  return <span
    title={inherited ? 'Protected by a group above this machine — stop waves skip it.' : 'Stop waves can never touch this machine.'}
    className="inline-flex items-center gap-1 rounded-full border border-sky-200 bg-sky-50 px-1.5 py-0 text-[11px] font-medium text-sky-800"
  >
    <ShieldCheck size={10} aria-hidden="true" />
    Never stop
  </span>
}

/**
 * Start/Stop picker. Deliberately a pair of buttons rather than a dropdown: choosing the wrong one
 * causes an outage, so the current choice has to be readable without opening anything.
 */
export function ActionPicker({ value, onChange, disabled }: { value: ScheduleAction; onChange: (next: ScheduleAction) => void; disabled?: boolean }) {
  return <div className="inline-flex rounded-lg border border-slate-300 p-0.5" role="radiogroup" aria-label="What this schedule does">
    {(['start', 'stop'] as const).map((option) => {
      const meta = actionMeta(option)
      const Icon = meta.icon
      const active = value === option
      return <button
        key={option}
        type="button"
        role="radio"
        aria-checked={active}
        disabled={disabled}
        onClick={() => onChange(option)}
        className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-semibold transition disabled:opacity-50 ${active ? `${meta.chip} border` : 'border border-transparent text-slate-600 hover:bg-slate-50'}`}
      >
        <Icon size={14} aria-hidden="true" />
        {meta.label}
      </button>
    })}
  </div>
}

/** Renders only the select: Field binds its label to a single child, so help text goes in the hint. */
export function StopModePicker({ value, onChange }: { value: StopMode; onChange: (next: StopMode) => void }) {
  return <select value={value} onChange={(event) => onChange(event.target.value as StopMode)}>
    <option value="deallocate">{STOP_MODE_LABEL.deallocate}</option>
    <option value="power_off">{STOP_MODE_LABEL.power_off}</option>
  </select>
}

export function RingOrderPicker({ value, onChange }: { value: RingOrder; onChange: (next: RingOrder) => void }) {
  return <select value={value} onChange={(event) => onChange(event.target.value as RingOrder)}>
    <option value="reverse">{RING_ORDER_LABEL.reverse}</option>
    <option value="sequence">{RING_ORDER_LABEL.sequence}</option>
  </select>
}
