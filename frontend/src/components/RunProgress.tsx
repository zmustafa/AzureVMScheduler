import type { ScheduleRun } from '../types'
import { formatDuration } from '../lib/time'

/** Run statuses that will never change again. */
const TERMINAL_RUN_STATUSES = new Set(['succeeded', 'failed', 'partially_failed', 'timed_out', 'cancelled'])

export function isRunActive(run: Pick<ScheduleRun, 'status' | 'finished_at'>): boolean {
  return !run.finished_at && !TERMINAL_RUN_STATUSES.has(run.status)
}

export function isRunFailed(run: Pick<ScheduleRun, 'status'>): boolean {
  return run.status === 'failed' || run.status === 'partially_failed' || run.status === 'timed_out'
}

/** Attempts that have reached a final state. */
export function completedCount(run: Pick<ScheduleRun, 'succeeded_count' | 'failed_count' | 'skipped_count'>): number {
  return run.succeeded_count + run.failed_count + run.skipped_count
}

/** "24/30 succeeded · 6 failed · 1 skipped" — always textual, never colour-only. */
export function countsText(run: Pick<ScheduleRun, 'total_count' | 'succeeded_count' | 'failed_count' | 'skipped_count'>): string {
  const parts = [`${run.succeeded_count}/${run.total_count} succeeded`]
  if (run.failed_count) parts.push(`${run.failed_count} failed`)
  if (run.skipped_count) parts.push(`${run.skipped_count} skipped`)
  return parts.join(' · ')
}

/** Difference between the planned and the actual start, as a signed label. */
export function latenessText(run: Pick<ScheduleRun, 'scheduled_for' | 'started_at'>): string | null {
  if (!run.scheduled_for || !run.started_at) return null
  const delta = new Date(run.started_at).getTime() - new Date(run.scheduled_for).getTime()
  if (!Number.isFinite(delta) || Math.abs(delta) < 30_000) return 'on time'
  return delta > 0 ? `${formatDuration(delta)} late` : `${formatDuration(delta)} early`
}

export function runDurationText(run: Pick<ScheduleRun, 'started_at' | 'finished_at'>): string {
  if (!run.started_at || !run.finished_at) return '—'
  return formatDuration(new Date(run.finished_at).getTime() - new Date(run.started_at).getTime())
}

/** Segmented progress bar. Segments carry their own labels through the accessible text. */
export function RunProgress({ run, size = 'md' }: { run: Pick<ScheduleRun, 'total_count' | 'succeeded_count' | 'failed_count' | 'skipped_count'>; size?: 'sm' | 'md' }) {
  const total = Math.max(run.total_count, 1)
  const pct = (value: number) => `${Math.min((value / total) * 100, 100)}%`
  const done = completedCount(run)
  const percent = run.total_count ? Math.round((done / total) * 100) : 0
  return <div
    role="progressbar"
    aria-valuemin={0}
    aria-valuemax={100}
    aria-valuenow={percent}
    aria-valuetext={`${done} of ${run.total_count} virtual machines finished — ${countsText(run)}`}
    className={`flex w-full overflow-hidden rounded-full bg-slate-200 ${size === 'sm' ? 'h-1.5' : 'h-2.5'}`}
  >
    <span className="bg-emerald-500" style={{ width: pct(run.succeeded_count) }} />
    <span className="bg-rose-500" style={{ width: pct(run.failed_count) }} />
    <span className="bg-slate-400" style={{ width: pct(run.skipped_count) }} />
  </div>
}
