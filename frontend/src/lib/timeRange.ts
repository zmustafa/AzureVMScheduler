/** Time-window model shared by the run timeline and the run table. */

export type RelativeUnit = 'minutes' | 'hours' | 'days'

export type TimeRange =
  | { kind: 'preset'; preset: PresetId }
  | { kind: 'relative'; amount: number; unit: RelativeUnit }
  | { kind: 'absolute'; from: number; to: number }

export type ResolvedRange = { from: number; to: number; label: string }

export type PresetId =
  | 'last15m' | 'last60m' | 'last4h' | 'last24h' | 'last7d' | 'last30d'
  | 'today' | 'yesterday' | 'weekToDate' | 'monthToDate' | 'previousWeek' | 'previousMonth'

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

export const PRESETS: { id: PresetId; label: string }[] = [
  { id: 'last15m', label: 'Last 15 minutes' },
  { id: 'last60m', label: 'Last 60 minutes' },
  { id: 'last4h', label: 'Last 4 hours' },
  { id: 'last24h', label: 'Last 24 hours' },
  { id: 'last7d', label: 'Last 7 days' },
  { id: 'last30d', label: 'Last 30 days' },
  { id: 'today', label: 'Today' },
  { id: 'yesterday', label: 'Yesterday' },
  { id: 'weekToDate', label: 'Week to date' },
  { id: 'monthToDate', label: 'Month to date' },
  { id: 'previousWeek', label: 'Previous week' },
  { id: 'previousMonth', label: 'Previous month' },
]

const PRESET_LABELS = new Map(PRESETS.map((item) => [item.id, item.label]))

/** Midnight starting the day that contains `at`, in the browser's local zone. */
function startOfDay(at: number): number {
  const date = new Date(at)
  date.setHours(0, 0, 0, 0)
  return date.getTime()
}

/** Monday 00:00 of the week containing `at`. */
function startOfWeek(at: number): number {
  const date = new Date(startOfDay(at))
  const weekday = (date.getDay() + 6) % 7
  date.setDate(date.getDate() - weekday)
  return date.getTime()
}

function startOfMonth(at: number, monthOffset = 0): number {
  const date = new Date(at)
  date.setHours(0, 0, 0, 0)
  date.setDate(1)
  date.setMonth(date.getMonth() + monthOffset)
  return date.getTime()
}

export function unitMs(unit: RelativeUnit): number {
  return unit === 'minutes' ? MINUTE : unit === 'hours' ? HOUR : DAY
}

function resolvePreset(preset: PresetId, now: number): { from: number; to: number } {
  switch (preset) {
    case 'last15m': return { from: now - 15 * MINUTE, to: now }
    case 'last60m': return { from: now - HOUR, to: now }
    case 'last4h': return { from: now - 4 * HOUR, to: now }
    case 'last24h': return { from: now - DAY, to: now }
    case 'last7d': return { from: now - 7 * DAY, to: now }
    case 'last30d': return { from: now - 30 * DAY, to: now }
    case 'today': return { from: startOfDay(now), to: now }
    case 'yesterday': return { from: startOfDay(now) - DAY, to: startOfDay(now) }
    case 'weekToDate': return { from: startOfWeek(now), to: now }
    case 'monthToDate': return { from: startOfMonth(now), to: now }
    case 'previousWeek': return { from: startOfWeek(now) - 7 * DAY, to: startOfWeek(now) }
    case 'previousMonth': return { from: startOfMonth(now, -1), to: startOfMonth(now) }
  }
}

export const DEFAULT_RANGE: TimeRange = { kind: 'preset', preset: 'last24h' }

export function resolveRange(range: TimeRange, now: number): ResolvedRange {
  if (range.kind === 'preset') {
    return { ...resolvePreset(range.preset, now), label: PRESET_LABELS.get(range.preset) ?? 'Custom range' }
  }
  if (range.kind === 'relative') {
    const span = Math.max(range.amount, 1) * unitMs(range.unit)
    const plural = Math.max(range.amount, 1) === 1 ? range.unit.replace(/s$/, '') : range.unit
    return { from: now - span, to: now, label: `Last ${Math.max(range.amount, 1)} ${plural}` }
  }
  const from = Math.min(range.from, range.to)
  const to = Math.max(range.from, range.to)
  return { from, to, label: 'Custom range' }
}

/** "UTC-5" style offset badge for the zone the picker is showing times in. */
export function offsetLabel(zone: 'local' | 'utc', at = Date.now()): string {
  if (zone === 'utc') return 'UTC'
  const minutes = -new Date(at).getTimezoneOffset()
  if (minutes === 0) return 'UTC'
  const sign = minutes > 0 ? '+' : '-'
  const hours = Math.floor(Math.abs(minutes) / 60)
  const rest = Math.abs(minutes) % 60
  return `UTC${sign}${hours}${rest ? `:${String(rest).padStart(2, '0')}` : ''}`
}

/** Value for a `datetime-local` input, expressed in the picker's chosen zone. */
export function toInputValue(ms: number, zone: 'local' | 'utc'): string {
  const shifted = zone === 'utc' ? new Date(ms + new Date(ms).getTimezoneOffset() * MINUTE) : new Date(ms)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${shifted.getFullYear()}-${pad(shifted.getMonth() + 1)}-${pad(shifted.getDate())}T${pad(shifted.getHours())}:${pad(shifted.getMinutes())}`
}

/** Inverse of {@link toInputValue}. Returns NaN when the text is not a complete datetime. */
export function fromInputValue(value: string, zone: 'local' | 'utc'): number {
  const parsed = new Date(value)
  const ms = parsed.getTime()
  if (Number.isNaN(ms)) return Number.NaN
  return zone === 'utc' ? ms - parsed.getTimezoneOffset() * MINUTE : ms
}

/** Compact window text such as "7/11/2026, 11:49:45 AM → 7/11/2026, 8:13:43 PM". */
export function formatWindow(from: number, to: number, zone: 'local' | 'utc'): string {
  const options: Intl.DateTimeFormatOptions = {
    year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: '2-digit', second: '2-digit',
    timeZone: zone === 'utc' ? 'UTC' : undefined,
  }
  const format = (ms: number) => new Intl.DateTimeFormat(undefined, options).format(new Date(ms))
  return `${format(from)} → ${format(to)}`
}

/** Axis tick text: time of day for short windows, date for long ones. */
export function formatTick(ms: number, span: number, zone: 'local' | 'utc'): string {
  const timeZone = zone === 'utc' ? 'UTC' : undefined
  const options: Intl.DateTimeFormatOptions = span > 3 * DAY
    ? { month: 'short', day: 'numeric', timeZone }
    : span > DAY
      ? { month: 'short', day: 'numeric', hour: 'numeric', timeZone }
      : { hour: 'numeric', minute: '2-digit', timeZone }
  return new Intl.DateTimeFormat(undefined, options).format(new Date(ms))
}

/** Bucket width that keeps a window at roughly `target` columns, snapped to a readable step. */
export function bucketSize(span: number, target = 60): number {
  const steps = [MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY]
  const ideal = span / target
  return steps.find((step) => step >= ideal) ?? steps[steps.length - 1]
}
