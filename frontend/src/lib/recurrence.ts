import type { RecurrenceFrequency, ScheduleType } from '../types'

/**
 * The frequency picker is a UI concept; the API only knows four schedule types.
 *
 * "Advanced" and "Custom" both persist as a cron expression — they are two ways of writing the
 * same thing, so the builder simply reads and writes the cron string rather than keeping a second
 * copy of the recurrence that could drift out of step.
 */
export const FREQUENCIES: { value: RecurrenceFrequency; label: string }[] = [
  { value: 'one_time', label: 'One time' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'advanced', label: 'Advanced (recurrence builder)' },
  { value: 'cron', label: 'Custom (cron expression)' },
]

export function toScheduleType(frequency: RecurrenceFrequency): ScheduleType {
  return frequency === 'advanced' ? 'cron' : frequency
}

/** Monday-first for the operator; cron counts Sunday as 0. */
export const WEEKDAYS: { label: string; cron: number; index: number }[] = [
  { label: 'Mon', cron: 1, index: 0 },
  { label: 'Tue', cron: 2, index: 1 },
  { label: 'Wed', cron: 3, index: 2 },
  { label: 'Thu', cron: 4, index: 3 },
  { label: 'Fri', cron: 5, index: 4 },
  { label: 'Sat', cron: 6, index: 5 },
  { label: 'Sun', cron: 0, index: 6 },
]

export const CRON_PRESETS: { label: string; expression: string }[] = [
  { label: 'Hourly', expression: '0 * * * *' },
  { label: 'Daily 08:00', expression: '0 8 * * *' },
  { label: 'Weekdays 09:00', expression: '0 9 * * 1,2,3,4,5' },
  { label: 'Weekly Mon', expression: '0 8 * * 1' },
  { label: 'Monthly 1st', expression: '0 8 1 * *' },
]

export type BuilderUnit = 'hour' | 'day' | 'week' | 'month'

export const BUILDER_UNITS: { value: BuilderUnit; label: string }[] = [
  { value: 'hour', label: 'hour' },
  { value: 'day', label: 'day' },
  { value: 'week', label: 'week (on days…)' },
  { value: 'month', label: 'month (on a date)' },
]

export type BuilderState = {
  unit: BuilderUnit
  minute: number
  /** Sorted, always contains at least the primary hour. Ignored when the unit is 'hour'. */
  hours: number[]
  /** Cron weekday numbers (0 = Sunday). Only used by the 'week' unit. */
  days: number[]
  /** Day of the month for the 'month' unit. */
  dayOfMonth: number
}

export const DEFAULT_BUILDER: BuilderState = { unit: 'week', minute: 0, hours: [9], days: [1, 2, 3, 4, 5], dayOfMonth: 1 }

const PLAIN_LIST = /^\d+(,\d+)*$/

function parseList(field: string): number[] | null {
  if (!PLAIN_LIST.test(field)) return null
  return [...new Set(field.split(',').map(Number))].sort((a, b) => a - b)
}

export function buildCron(state: BuilderState): string {
  const minute = clamp(state.minute, 0, 59)
  const hours = (state.hours.length ? [...new Set(state.hours)] : [0]).sort((a, b) => a - b).join(',')
  switch (state.unit) {
    case 'hour':
      return `${minute} * * * *`
    case 'day':
      return `${minute} ${hours} * * *`
    case 'month':
      return `${minute} ${hours} ${clamp(state.dayOfMonth, 1, 31)} * *`
    default:
      return `${minute} ${hours} * * ${(state.days.length ? [...new Set(state.days)] : [1]).sort((a, b) => a - b).join(',')}`
  }
}

/**
 * Read a cron string back into builder controls, or null if the builder cannot express it.
 *
 * Only the canonical shapes the builder itself emits are accepted — ranges, steps and named days
 * are deliberately left to the raw editor rather than being silently rewritten.
 */
export function parseCronToBuilder(expression: string): BuilderState | null {
  const fields = expression.trim().split(/\s+/)
  if (fields.length !== 5) return null
  const [minuteField, hourField, domField, monthField, dowField] = fields
  if (monthField !== '*') return null
  const minutes = parseList(minuteField)
  if (!minutes || minutes.length !== 1) return null
  const minute = minutes[0]

  if (hourField === '*') {
    return domField === '*' && dowField === '*' ? { ...DEFAULT_BUILDER, unit: 'hour', minute } : null
  }
  const hours = parseList(hourField)
  if (!hours) return null

  if (domField === '*' && dowField === '*') return { ...DEFAULT_BUILDER, unit: 'day', minute, hours }
  if (domField === '*') {
    const days = parseList(dowField)
    return days ? { ...DEFAULT_BUILDER, unit: 'week', minute, hours, days } : null
  }
  if (dowField === '*') {
    const dom = parseList(domField)
    return dom && dom.length === 1 ? { ...DEFAULT_BUILDER, unit: 'month', minute, hours, dayOfMonth: dom[0] } : null
  }
  return null
}

/** Which editor a stored schedule should open in. */
export function frequencyOf(scheduleType: ScheduleType, cronExpression: string): RecurrenceFrequency {
  if (scheduleType !== 'cron') return scheduleType
  return parseCronToBuilder(cronExpression) ? 'advanced' : 'cron'
}

export function hoursOf(state: BuilderState): number[] {
  return [...new Set(state.hours)].sort((a, b) => a - b)
}

/** The builder's "At time" control owns the minute and the earliest hour. */
export function withPrimaryTime(state: BuilderState, hour: number, minute: number): BuilderState {
  const rest = state.hours.filter((value) => value !== Math.min(...(state.hours.length ? state.hours : [hour])))
  return { ...state, minute, hours: [...new Set([hour, ...rest])].sort((a, b) => a - b) }
}

export function primaryHour(state: BuilderState): number {
  return state.hours.length ? Math.min(...state.hours) : 0
}

function clamp(value: number, low: number, high: number): number {
  return Number.isFinite(value) ? Math.min(Math.max(Math.trunc(value), low), high) : low
}

export function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/**
 * A short human summary for list rows and headings.
 *
 * Deliberately mirrors the server's `describe`, but computed locally so a table of 50 schedules
 * does not need 50 preview round trips.
 */
export function recurrenceSummary(schedule: {
  schedule_type: ScheduleType
  start_time: string
  cron_expression?: string | null
  weekday?: number | null
}): string {
  switch (schedule.schedule_type) {
    case 'one_time':
      return `Once at ${schedule.start_time}`
    case 'daily':
      return `Daily at ${schedule.start_time}`
    case 'weekly':
      return `Weekly on ${WEEKDAYS[schedule.weekday ?? 0]?.label ?? '?'} at ${schedule.start_time}`
    default:
      return schedule.cron_expression || 'Cron'
  }
}

/** Bounds worth surfacing next to the recurrence, e.g. "until 2026-12-31 · max 10 runs". */
export function recurrenceBounds(schedule: { start_date?: string | null; end_date?: string | null; run_limit?: number | null; run_count?: number }): string {
  const parts: string[] = []
  if (schedule.start_date) parts.push(`from ${schedule.start_date}`)
  if (schedule.end_date) parts.push(`until ${schedule.end_date}`)
  if (schedule.run_limit) parts.push(`run ${schedule.run_count ?? 0} of ${schedule.run_limit}`)
  return parts.join(' · ')
}
