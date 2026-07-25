import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, CalendarClock, Layers, Play, RotateCcw, Save, Server, Trash2 } from 'lucide-react'
import { api, json } from '../api'
import { useCan } from '../auth'
import { connectionLabel, useGroupTree } from '../lib/queries'
import { formatInZone, useCountdown, useDisplayTimezone, zoneLabel } from '../lib/time'
import { ScheduleFields, scheduleToForm, scheduleToPayload, type ScheduleFormState } from '../components/ScheduleDrawer'
import { ConfirmDialog } from '../components/Overlay'
import { StatusBadge } from '../components/StatusBadge'
import { ActionBadge } from '../components/ActionBits'
import { actionMeta, actionSentence } from '../lib/actions'
import { recurrenceBounds, recurrenceSummary } from '../lib/recurrence'
import { Chip, ErrorNotice, Loading, PageHeader } from '../components/Ui'
import type { ScheduleDetail, ScheduleRun } from '../types'

/** Schedule configuration plus its run and attempt history. */
export function ScheduleDetailPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const client = useQueryClient()
  const canWrite = useCan('schedules.write')
  const tree = useGroupTree()
  const { format, localZone } = useDisplayTimezone()

  const query = useQuery({ queryKey: ['schedule', id], queryFn: () => api<ScheduleDetail>(`/schedules/${id}`), enabled: !!id })
  const [form, setForm] = useState<ScheduleFormState | null>(null)
  const [confirmRun, setConfirmRun] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  useEffect(() => { if (query.data) setForm(scheduleToForm(query.data.schedule)) }, [query.data])

  const invalidate = () => {
    void client.invalidateQueries({ queryKey: ['schedule', id] })
    void client.invalidateQueries({ queryKey: ['schedules'] })
    void client.invalidateQueries({ queryKey: ['runs'] })
  }
  const save = useMutation({ mutationFn: () => api(`/schedules/${id}`, json('PATCH', form ? scheduleToPayload(form) : {})), onSuccess: invalidate })
  const remove = useMutation({ mutationFn: () => api(`/schedules/${id}`, json('DELETE')), onSuccess: () => navigate('/schedules') })
  const run = useMutation({ mutationFn: () => api<ScheduleRun | null>(`/schedules/${id}/run`, json('POST')), onSuccess: () => { invalidate(); setConfirmRun(false) } })
  const retry = useMutation({ mutationFn: (attemptId: string) => api<ScheduleRun | null>(`/attempts/${attemptId}/retry`, json('POST')), onSuccess: invalidate })

  const countdown = useCountdown(query.data?.schedule.next_run_at ?? null, query.data?.schedule.timezone)

  if (query.isLoading) return <Loading />
  if (query.error || !query.data) return <ErrorNotice error={query.error ?? new Error('Schedule not found')} />

  const { schedule, vms, attempts, runs } = query.data

  return <>
    <Link to="/schedules" className="mb-4 inline-flex items-center gap-2 text-sm text-slate-600 hover:text-blue-700"><ArrowLeft size={16} />Schedules</Link>
    <PageHeader
      title={schedule.name}
      description={`${recurrenceSummary(schedule)} · ${schedule.timezone} (${zoneLabel(schedule.timezone, schedule.next_run_at)}) — ${actionMeta(schedule.action).verb}s virtual machines`}
      action={<div className="flex flex-wrap items-center gap-2">
        <ActionBadge action={schedule.action} stopMode={schedule.stop_mode} />
        <StatusBadge value={schedule.status} />
        <Chip tone="info" icon={<CalendarClock size={13} />}>{schedule.next_run_at ? countdown : 'Not scheduled'}</Chip>
        {canWrite && <button type="button" className="btn-primary" onClick={() => setConfirmRun(true)}><Play size={15} />Run now</button>}
      </div>}
    />

    {(run.error || retry.error) && <div className="mb-4"><ErrorNotice error={run.error ?? retry.error} /></div>}

    <section className="card mb-6 grid gap-3 sm:grid-cols-3">
      <div><p className="muted">Next occurrence · schedule zone</p><p className="mt-1 text-sm font-medium text-slate-800">{formatInZone(schedule.next_run_at, schedule.timezone)}</p></div>
      <div><p className="muted">Next occurrence · your zone</p><p className="mt-1 text-sm font-medium text-slate-800">{formatInZone(schedule.next_run_at, localZone)}</p></div>
      <div><p className="muted">Next occurrence · UTC</p><p className="mt-1 text-sm font-medium text-slate-800">{formatInZone(schedule.next_run_at, 'UTC')}</p></div>
      <div className="sm:col-span-3 flex flex-wrap items-center gap-2 border-t border-slate-200 pt-3">
        <Chip tone="neutral" icon={schedule.target_type === 'group' ? <Layers size={13} /> : <Server size={13} />}>{schedule.target_label ?? schedule.target_id}</Chip>
        <Chip tone="neutral">{vms.length} VM{vms.length === 1 ? '' : 's'} resolved</Chip>
        <Chip tone="neutral">Stagger {schedule.stagger_seconds}s</Chip>
        {recurrenceBounds(schedule) && <Chip tone="info">{recurrenceBounds(schedule)}</Chip>}
        <Chip tone="neutral">{connectionLabel(schedule.connection_name, schedule.connection_tenant_id)}</Chip>
      </div>
    </section>

    <div className="grid gap-6 xl:grid-cols-[1fr_1fr]">
      <section className="card space-y-4">
        <div><h2 className="font-semibold text-slate-900">Configuration</h2><p className="muted">Changes apply to the next occurrence.</p></div>
        {save.error && <ErrorNotice error={save.error} />}
        {form && <ScheduleFields value={form} onChange={setForm} tree={tree.data ?? []} />}
        <div className="flex justify-between gap-3 border-t border-slate-200 pt-4">
          {canWrite && <button type="button" className="btn-danger" onClick={() => setConfirmDelete(true)}><Trash2 size={16} />Delete</button>}
          {canWrite && <button type="button" className="btn-primary" disabled={save.isPending || !form} onClick={() => save.mutate()}><Save size={16} />{save.isPending ? 'Saving…' : 'Save changes'}</button>}
        </div>
      </section>

      <div className="space-y-6">
        <section className="card">
          <h2 className="font-semibold text-slate-900">Recent runs</h2>
          <div className="mt-3 divide-y divide-slate-200">
            {runs.length ? runs.map((item) => <article key={item.id} className="flex flex-wrap items-center justify-between gap-2 py-3">
              <div className="min-w-0">
                <Link className="text-sm font-medium text-slate-900 hover:text-blue-700" to={`/runs/${item.id}`}>{item.trigger === 'manual' ? 'Manual run' : 'Scheduled run'}</Link>
                <p className="text-xs text-slate-500">{format(item.created_at, schedule.timezone)}</p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Chip tone="neutral">{item.succeeded_count}/{item.total_count} succeeded</Chip>
                <StatusBadge value={item.status} />
              </div>
            </article>) : <p className="py-8 text-center muted">No runs yet.</p>}
          </div>
        </section>

        <section className="card">
          <h2 className="font-semibold text-slate-900">{actionMeta(schedule.action).label} attempts</h2>
          <p className="muted">Every scheduler claim records its mode and outcome.</p>
          <div className="mt-3 max-h-[32rem] divide-y divide-slate-200 overflow-y-auto">
            {attempts.length ? attempts.map((item) => <article className="py-3" key={item.id}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="font-mono text-xs text-slate-600">#{item.attempt_number} · {item.correlation_id.slice(0, 8)}</p>
                <div className="flex items-center gap-2">
                  <StatusBadge value={item.mode} />
                  <StatusBadge value={item.status} />
                  {canWrite && ['failed', 'timed_out'].includes(item.status) && <button type="button" className="btn-secondary !py-1" disabled={retry.isPending} onClick={() => retry.mutate(item.id)}><RotateCcw size={14} />Retry</button>}
                </div>
              </div>
              <p className="mt-1.5 break-all text-xs text-slate-500">{item.vm_resource_id}</p>
              <p className="mt-1 text-sm text-slate-700">{item.message || 'No message recorded.'}</p>
              <p className="mt-1 text-xs text-slate-500">Claimed {format(item.claimed_at, schedule.timezone)} · Completed {format(item.completed_at, schedule.timezone)}</p>
            </article>) : <p className="py-8 text-center muted">No attempts yet.</p>}
          </div>
        </section>
      </div>
    </div>

    <ConfirmDialog open={confirmRun} tone={schedule.action === 'stop' ? 'danger' : 'primary'} title={`${actionMeta(schedule.action).label} virtual machines now`} confirmLabel="Run now" busy={run.isPending} onCancel={() => setConfirmRun(false)} onConfirm={() => run.mutate()}>
      <p>This will {actionSentence(schedule.action, schedule.stop_mode, vms.length)} for <strong>{schedule.target_label ?? schedule.target_id}</strong>.</p>
      <p className="mt-2">Tenant: <strong>{connectionLabel(schedule.connection_name, schedule.connection_tenant_id)}</strong>.</p>
    </ConfirmDialog>
    <ConfirmDialog open={confirmDelete} title="Delete schedule" confirmLabel="Delete schedule" busy={remove.isPending} onCancel={() => setConfirmDelete(false)} onConfirm={() => remove.mutate()}>
      <p><strong>{schedule.name}</strong> will be removed. Its run history stays in the audit log.</p>
    </ConfirmDialog>
  </>
}
