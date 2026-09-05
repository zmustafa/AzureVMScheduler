import { useMemo } from 'react'
import { Link } from 'react-router'
import { Pin, PinOff } from 'lucide-react'
import { useDisplayTimezone } from '../lib/time'
import { ActionBadge } from './ActionBits'
import { Chip } from './Ui'
import type { ApplicationHealth, RolloutPlan } from '../types'

const CELL = {
  succeeded: 'bg-emerald-500',
  partially_failed: 'bg-amber-500',
  failed: 'bg-rose-500',
  timed_out: 'bg-rose-500',
  cancelled: 'bg-slate-400',
  skipped: 'bg-slate-300',
  running: 'bg-blue-400 animate-pulse',
  pending: 'bg-slate-300',
} as const

/** One row per application, one cell per recent wave — a week of history at a glance. */
export function HealthMatrix({ applications, pinned, onTogglePin, canOpenApplications = true, canOpenRuns = true }: {
  applications: ApplicationHealth[]
  pinned: string[]
  onTogglePin: (id: string) => void
  canOpenApplications?: boolean
  canOpenRuns?: boolean
}) {
  const { format } = useDisplayTimezone()
  const ordered = useMemo(() => {
    const rank = (item: ApplicationHealth) => (pinned.includes(item.id) ? 0 : 1)
    return [...applications].sort((a, b) => rank(a) - rank(b) || b.failed_runs - a.failed_runs || a.name.localeCompare(b.name))
  }, [applications, pinned])

  if (ordered.length === 0) return <p className="py-8 text-center muted">No applications yet.</p>

  return <ul className="space-y-2">
    {ordered.map((app) => {
      const gap = app.vm_count - app.covered_vm_count
      const isPinned = pinned.includes(app.id)
      return <li key={app.id} className="rounded-lg border border-slate-200 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <button
              type="button"
              onClick={() => onTogglePin(app.id)}
              aria-pressed={isPinned}
              aria-label={isPinned ? `Unpin ${app.name}` : `Pin ${app.name}`}
              className={`shrink-0 transition ${isPinned ? 'text-blue-700' : 'text-slate-300 hover:text-slate-500'}`}
            >{isPinned ? <Pin size={14} /> : <PinOff size={14} />}</button>
            {canOpenApplications ? <Link className="link truncate font-medium" to={`/applications/${app.id}`}>{app.name}</Link> : <span className="truncate font-medium text-slate-800">{app.name}</span>}
            {!app.enabled && <Chip tone="neutral">Disabled</Chip>}
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {gap > 0 && <Chip tone="warn" title={`${gap} virtual machine${gap === 1 ? '' : 's'} are not covered by any schedule`}>{gap} uncovered</Chip>}
            {app.failed_runs > 0 && <Chip tone="danger">{app.failed_runs} failed</Chip>}
            <span className="text-xs text-slate-500">{app.covered_vm_count}/{app.vm_count} VMs · {app.ring_count} rings</span>
          </div>
        </div>
        <div className="mt-2 flex items-center gap-1">
          {app.recent.length === 0
            ? <span className="text-xs text-slate-400">No waves in this window</span>
            : [...app.recent].reverse().map((run) => {
              const className = `h-5 w-8 rounded-sm ${CELL[run.status as keyof typeof CELL] ?? 'bg-slate-300'}`
              const title = `${run.status.replaceAll('_', ' ')} — ${run.succeeded}/${run.total} succeeded · ${format(run.at)}`
              return canOpenRuns
                ? <Link key={run.run_id} to={`/runs/${run.run_id}`} title={title} aria-label={`Run ${run.status.replaceAll('_', ' ')} at ${format(run.at)}`} className={`${className} transition hover:opacity-80`} />
                : <span key={run.run_id} title={title} className={className} />
            })}
        </div>
      </li>
    })}
  </ul>
}

/** Tonight's plan: each application's rings in start order, with the window they occupy. */
export function RolloutPlanPanel({ plans, canOpenApplications = true, canOpenSchedules = true }: { plans: RolloutPlan[]; canOpenApplications?: boolean; canOpenSchedules?: boolean }) {
  const { format } = useDisplayTimezone()
  if (plans.length === 0) return <p className="py-8 text-center muted">No enabled schedule has a next occurrence.</p>

  return <ul className="space-y-3">
    {plans.map((plan) => {
      // Show the application summary in the same zone as its waves, or the times look contradictory.
      const zone = plan.waves[0]?.timezone
      return <li key={plan.id} className="rounded-lg border border-slate-200 p-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          {canOpenApplications ? <Link className="link font-medium" to={`/applications/${plan.id}`}>{plan.name}</Link> : <span className="font-medium text-slate-800">{plan.name}</span>}
          <p className="text-xs text-slate-600">
            {format(plan.starts_at, zone)}
            {plan.finishes_at && plan.finishes_at !== plan.starts_at && <> → {format(plan.finishes_at, zone)}</>}
            <span className="ml-2 text-slate-500">{plan.vm_count} VM{plan.vm_count === 1 ? '' : 's'}</span>
          </p>
        </div>
        <ol className="mt-2 space-y-1">
          {plan.waves.map((wave, index) => <li key={wave.schedule_id} className="flex flex-wrap items-center gap-2 text-xs">
            <span className="w-4 shrink-0 text-center font-semibold text-slate-400">{index + 1}</span>
            <ActionBadge action={wave.action} stopMode={wave.stop_mode} size="sm" />
            {canOpenSchedules ? <Link className="link truncate" to={`/schedules/${wave.schedule_id}`}>{wave.target}</Link> : <span className="truncate text-slate-700">{wave.target}</span>}
            <span className="tabular-nums text-slate-700">{format(wave.next_run_at, wave.timezone)}</span>
            <Chip>{wave.vm_count} VM{wave.vm_count === 1 ? '' : 's'}</Chip>
            {wave.stagger_seconds > 0 && <span className="text-slate-500">{wave.stagger_seconds}s stagger</span>}
          </li>)}
        </ol>
      </li>
    })}
  </ul>
}
