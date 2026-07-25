import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CalendarClock, CircleAlert } from 'lucide-react'

import { api, json } from '../api'
import {
  BUILDER_UNITS,
  CRON_PRESETS,
  DEFAULT_BUILDER,
  FREQUENCIES,
  WEEKDAYS,
  buildCron,
  hoursOf,
  pad,
  parseCronToBuilder,
  primaryHour,
  toScheduleType,
  withPrimaryTime,
  type BuilderState,
} from '../lib/recurrence'
import { countdownText, serverNow, useDisplayTimezone } from '../lib/time'
import { TimezonePicker } from './TimezonePicker'
import { Field } from './Ui'
import type { RecurrenceFrequency, RecurrencePreview } from '../types'

/** The calendar half of a schedule form. Kept separate so the drawer stays readable. */
export type RecurrenceValue = {
  frequency: RecurrenceFrequency
  start_time: string
  cron_expression: string
  weekday: number | null
  timezone: string
  start_date: string
  end_date: string
  run_limit: number | null
}

const HOURS = Array.from({ length: 24 }, (_, hour) => hour)

/** A small toggle used for weekday and hour selection. */
function Chip({ active, label, onClick, title }: { active: boolean; label: string; onClick: () => void; title?: string }) {
  return <button
    type="button"
    aria-pressed={active}
    title={title}
    onClick={onClick}
    className={`rounded-md border px-2 py-1 text-xs font-medium tabular-nums transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure ${active ? 'border-blue-400 bg-blue-50 text-blue-800' : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50'}`}
  >{label}</button>
}

function RecurrenceBuilder({ cron, onChange }: { cron: string; onChange: (expression: string) => void }) {
  // The builder is a view over the cron string, so there is only ever one copy of the recurrence.
  const state = useMemo(() => parseCronToBuilder(cron) ?? DEFAULT_BUILDER, [cron])
  const patch = (next: Partial<BuilderState>) => onChange(buildCron({ ...state, ...next }))
  const hours = hoursOf(state)
  const primary = primaryHour(state)

  return <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-sm text-slate-700">Repeat every</span>
      <select className="!w-auto" value={state.unit} onChange={(event) => patch({ unit: event.target.value as BuilderState['unit'] })} aria-label="Repeat every">
        {BUILDER_UNITS.map((unit) => <option key={unit.value} value={unit.value}>{unit.label}</option>)}
      </select>
    </div>

    {state.unit === 'week' && <div>
      <p className="mb-1.5 text-sm text-slate-700">On days</p>
      <div className="flex flex-wrap gap-1.5">
        {WEEKDAYS.map((day) => <Chip
          key={day.cron}
          label={day.label}
          active={state.days.includes(day.cron)}
          onClick={() => {
            const days = state.days.includes(day.cron) ? state.days.filter((value) => value !== day.cron) : [...state.days, day.cron]
            patch({ days: days.length ? days : [day.cron] })
          }}
        />)}
      </div>
    </div>}

    {state.unit === 'month' && <Field label="On day of the month">
      <input
        type="number"
        min={1}
        max={31}
        className="!w-24"
        value={state.dayOfMonth}
        onChange={(event) => patch({ dayOfMonth: Number(event.target.value) })}
      />
    </Field>}

    <div className="flex flex-wrap items-center gap-2">
      <span className="text-sm text-slate-700">{state.unit === 'hour' ? 'At minute' : 'At time'}</span>
      {state.unit === 'hour'
        ? <input type="number" min={0} max={59} className="!w-20" aria-label="At minute" value={state.minute} onChange={(event) => patch({ minute: Number(event.target.value) })} />
        : <input
            type="time"
            className="!w-32"
            aria-label="At time"
            value={`${pad(primary)}:${pad(state.minute)}`}
            onChange={(event) => {
              const [hour, minute] = event.target.value.split(':').map(Number)
              if (Number.isFinite(hour) && Number.isFinite(minute)) onChange(buildCron(withPrimaryTime(state, hour, minute)))
            }}
          />}
    </div>

    {state.unit !== 'hour' && <div>
      <p className="mb-1.5 text-sm text-slate-700">Also at these hours <span className="text-xs text-slate-500">(optional — same minute)</span></p>
      <div className="flex flex-wrap gap-1">
        {HOURS.map((hour) => <Chip
          key={hour}
          label={pad(hour)}
          active={hours.includes(hour)}
          title={hour === primary ? 'The time above sets this hour' : undefined}
          onClick={() => {
            if (hour === primary) return // owned by the time field, so it cannot be toggled off here
            const next = hours.includes(hour) ? hours.filter((value) => value !== hour) : [...hours, hour]
            patch({ hours: next.length ? next : [primary] })
          }}
        />)}
      </div>
    </div>}

    <p className="font-mono text-xs text-slate-600">Cron: {cron}</p>
  </div>
}

function PreviewPanel({ value }: { value: RecurrenceValue }) {
  const { format } = useDisplayTimezone()
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), 250)
    return () => clearTimeout(timer)
  }, [value])

  const body = {
    schedule_type: toScheduleType(debounced.frequency),
    start_time: debounced.start_time,
    cron_expression: debounced.cron_expression,
    weekday: debounced.weekday,
    timezone: debounced.timezone,
    start_date: debounced.start_date,
    end_date: debounced.end_date,
    run_limit: debounced.run_limit,
  }
  const preview = useQuery({
    queryKey: ['schedule-preview', body],
    queryFn: () => api<RecurrencePreview>('/schedules/preview', json('POST', body)),
    // The recurrence is pure input, so a stale answer is never useful.
    staleTime: 0,
    retry: false,
  })

  if (preview.isLoading && !preview.data) {
    return <p className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm text-slate-500">Working out the next runs…</p>
  }
  const data = preview.data
  if (!data || !data.valid) {
    return <p className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5 text-sm text-amber-900">
      <CircleAlert size={15} className="mt-0.5 shrink-0" aria-hidden="true" />
      {data?.error || 'This recurrence is not valid yet.'}
    </p>
  }

  return <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5" aria-live="polite">
    <p className="flex flex-wrap items-center gap-x-1.5 text-sm text-slate-800">
      <CalendarClock size={14} className="shrink-0 text-blue-600" aria-hidden="true" />
      <span className="font-medium">{data.description}</span>
      {data.next_run_at && <>
        <span className="text-slate-400">·</span>
        <span>Next run {format(data.next_run_at, debounced.timezone)}</span>
        <span className="text-blue-700">({countdownText(data.next_run_at, serverNow(), debounced.timezone)})</span>
      </>}
    </p>
    {data.upcoming.length > 1 && <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
      <span className="font-medium text-slate-600">Upcoming:</span>
      {data.upcoming.map((moment) => <span key={moment} className="tabular-nums">{format(moment, debounced.timezone)}</span>)}
    </p>}
  </div>
}

export function RecurrenceEditor({ value, onChange }: { value: RecurrenceValue; onChange: (next: RecurrenceValue) => void }) {
  const patch = (partial: Partial<RecurrenceValue>) => onChange({ ...value, ...partial })
  const recurring = value.frequency !== 'one_time'

  /** Moving between modes has to leave a valid recurrence behind, not a half-filled one. */
  const changeFrequency = (frequency: RecurrenceFrequency) => {
    const next: Partial<RecurrenceValue> = { frequency }
    if (frequency === 'one_time') next.start_time = ''
    if (frequency === 'daily' || frequency === 'weekly') next.start_time = value.start_time && value.start_time.includes(':') && !value.start_time.includes('T') ? value.start_time : '08:00'
    if (frequency === 'weekly' && value.weekday === null) next.weekday = 0
    if (isCronFrequency(frequency) && !value.cron_expression) next.cron_expression = frequency === 'advanced' ? buildCron(DEFAULT_BUILDER) : '0 8 * * *'
    onChange({ ...value, ...next })
  }

  return <>
    <Field label="Frequency">
      <select value={value.frequency} onChange={(event) => changeFrequency(event.target.value as RecurrenceFrequency)}>
        {FREQUENCIES.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
    </Field>

    {value.frequency === 'one_time' && <Field label="Start" hint="Runs once at this local date and time.">
      <input type="datetime-local" autoComplete="off" value={value.start_time} onChange={(event) => patch({ start_time: event.target.value })} required />
    </Field>}

    {(value.frequency === 'daily' || value.frequency === 'weekly') && <Field label="Time of day">
      <input type="time" autoComplete="off" value={value.start_time} onChange={(event) => patch({ start_time: event.target.value })} required />
    </Field>}

    {value.frequency === 'weekly' && <Field label="Weekday">
      <select value={value.weekday ?? 0} onChange={(event) => patch({ weekday: Number(event.target.value) })}>
        {WEEKDAYS.map((day) => <option key={day.index} value={day.index}>{day.label}</option>)}
      </select>
    </Field>}

    {value.frequency === 'cron' && <div className="md:col-span-2">
      {/* The input is a direct child so Field can bind the label to it; presets sit alongside. */}
      <Field label="Cron expression" hint="Five fields: minute hour day-of-month month day-of-week.">
        <input className="font-mono" autoComplete="off" spellCheck={false} value={value.cron_expression} onChange={(event) => patch({ cron_expression: event.target.value })} placeholder="0 9 * * 1,2,3,4,5" required />
      </Field>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {CRON_PRESETS.map((preset) => <button
          key={preset.label}
          type="button"
          className="rounded border border-slate-200 bg-white px-1.5 py-0.5 font-mono text-[11px] text-slate-600 transition hover:bg-slate-50"
          onClick={() => patch({ cron_expression: preset.expression })}
        >{preset.label}</button>)}
      </div>
    </div>}

    <Field label="Timezone">
      <TimezonePicker value={value.timezone} onChange={(zone) => patch({ timezone: zone })} />
    </Field>

    {recurring && <Field label="Run limit (optional)" hint="Stops after this many scheduled runs. Manual runs are free.">
      <input
        type="number"
        min={1}
        autoComplete="off"
        placeholder="∞"
        value={value.run_limit ?? ''}
        onChange={(event) => patch({ run_limit: event.target.value ? Number(event.target.value) : null })}
      />
    </Field>}

    {recurring && <Field label="Start date (optional)">
      <input type="date" autoComplete="off" value={value.start_date} onChange={(event) => patch({ start_date: event.target.value })} />
    </Field>}

    {recurring && <Field label="End date (optional)">
      <input type="date" autoComplete="off" value={value.end_date} onChange={(event) => patch({ end_date: event.target.value })} />
    </Field>}

    {value.frequency === 'advanced' && <div className="md:col-span-2">
      <RecurrenceBuilder cron={value.cron_expression} onChange={(expression) => patch({ cron_expression: expression })} />
    </div>}

    <div className="md:col-span-2"><PreviewPanel value={value} /></div>
  </>
}

function isCronFrequency(frequency: RecurrenceFrequency): boolean {
  return frequency === 'advanced' || frequency === 'cron'
}
