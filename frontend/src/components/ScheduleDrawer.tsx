import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Server, Layers } from 'lucide-react'
import { api, json } from '../api'
import { connectionOptionLabel, findGroup, staggerHint, useConnections, useGeneralSettings, useGroupTree } from '../lib/queries'
import { frequencyOf, toScheduleType } from '../lib/recurrence'
import { STOP_MODE_HELP } from '../lib/actions'
import { Drawer } from './Overlay'
import { ActionPicker, RingOrderPicker, StopModePicker } from './ActionBits'
import { GroupPicker } from './GroupPicker'
import { RecurrenceEditor } from './RecurrenceEditor'
import { ErrorNotice, Field, SearchInput } from './Ui'
import type { GroupNode, Paged, RecurrenceFrequency, RingOrder, Schedule, ScheduleAction, StopMode, TargetType, VirtualMachine } from '../types'

export type ScheduleFormState = {
  name: string
  action: ScheduleAction
  stop_mode: StopMode
  ring_order: RingOrder
  frequency: RecurrenceFrequency
  start_time: string
  cron_expression: string
  weekday: number | null
  timezone: string
  start_date: string
  end_date: string
  run_limit: number | null
  target_type: TargetType
  target_id: string
  stagger_seconds: number
  azure_connection_id: string
  enabled: boolean
  notes: string
}

export function scheduleToForm(schedule: Schedule): ScheduleFormState {
  return {
    name: schedule.name,
    action: schedule.action ?? 'start',
    stop_mode: schedule.stop_mode ?? 'deallocate',
    ring_order: schedule.ring_order ?? 'sequence',
    frequency: frequencyOf(schedule.schedule_type, schedule.cron_expression ?? ''),
    start_time: schedule.start_time,
    cron_expression: schedule.cron_expression ?? '',
    weekday: schedule.weekday ?? null,
    timezone: schedule.timezone,
    start_date: schedule.start_date ?? '',
    end_date: schedule.end_date ?? '',
    run_limit: schedule.run_limit ?? null,
    target_type: schedule.target_type,
    target_id: schedule.target_id,
    stagger_seconds: schedule.stagger_seconds,
    azure_connection_id: schedule.azure_connection_id ?? '',
    enabled: schedule.enabled,
    notes: schedule.notes,
  }
}

export function emptyScheduleForm(timezone: string, target?: { type: TargetType; id: string }): ScheduleFormState {
  return {
    name: '', action: 'start', stop_mode: 'deallocate', ring_order: 'sequence',
    frequency: 'daily', start_time: '07:00', cron_expression: '', weekday: null,
    timezone, start_date: '', end_date: '', run_limit: null,
    target_type: target?.type ?? 'group', target_id: target?.id ?? '',
    stagger_seconds: 10, azure_connection_id: '', enabled: true, notes: '',
  }
}

export function scheduleToPayload(form: ScheduleFormState) {
  const scheduleType = toScheduleType(form.frequency)
  return {
    name: form.name.trim(),
    action: form.action,
    stop_mode: form.stop_mode,
    ring_order: form.ring_order,
    schedule_type: scheduleType,
    start_time: scheduleType === 'cron' ? '' : form.start_time,
    cron_expression: scheduleType === 'cron' ? form.cron_expression.trim() : '',
    weekday: scheduleType === 'weekly' ? form.weekday ?? 0 : null,
    timezone: form.timezone,
    start_date: scheduleType === 'one_time' ? '' : form.start_date,
    end_date: scheduleType === 'one_time' ? '' : form.end_date,
    run_limit: scheduleType === 'one_time' ? null : form.run_limit,
    target_type: form.target_type,
    target_id: form.target_id,
    stagger_seconds: Number(form.stagger_seconds) || 0,
    azure_connection_id: form.azure_connection_id || null,
    enabled: form.enabled,
    notes: form.notes,
  }
}

function targetVmCount(form: ScheduleFormState, tree: GroupNode[]): number {
  if (form.target_type === 'vm') return form.target_id ? 1 : 0
  const node = form.target_id ? findGroup(tree, form.target_id) : undefined
  return node?.subtree_vm_count ?? 0
}

function VmTargetPicker({ value, onChange }: { value: string; onChange: (id: string) => void }) {
  const [query, setQuery] = useState('')
  const results = useQuery({ queryKey: ['vms', 'picker', query], queryFn: () => api<Paged<VirtualMachine>>(`/vms?limit=25${query ? `&q=${encodeURIComponent(query)}` : ''}`) })
  return <div className="space-y-2">
    <SearchInput value={query} onChange={setQuery} placeholder="Search virtual machines" label="Search virtual machines" />
    <ul role="listbox" aria-label="Virtual machines" className="max-h-64 overflow-y-auto rounded-lg border border-slate-300 bg-white">
      {results.isLoading && <li className="px-3 py-3 text-sm text-slate-500">Loading virtual machines…</li>}
      {results.data?.items.length === 0 && <li className="px-3 py-3 text-sm text-slate-500">No virtual machines match this search.</li>}
      {results.data?.items.map((vm) => <li key={vm.id} role="option" aria-selected={vm.id === value}>
        <button type="button" onClick={() => onChange(vm.id)} className={`flex w-full flex-col items-start px-3 py-2 text-left transition ${vm.id === value ? 'bg-blue-100' : 'hover:bg-slate-50'}`}>
          <span className="text-sm font-medium text-slate-900">{vm.display_name || vm.vm_name}</span>
          <span className="text-xs text-slate-500">{vm.group_path} · {vm.resource_group}</span>
        </button>
      </li>)}
    </ul>
  </div>
}

/** Shared schedule editor fields — used by the create drawer and the schedule detail page. */
export function ScheduleFields({ value, onChange, tree, lockTarget }: { value: ScheduleFormState; onChange: (next: ScheduleFormState) => void; tree: GroupNode[]; lockTarget?: boolean }) {
  const connections = useConnections()
  const patch = (partial: Partial<ScheduleFormState>) => onChange({ ...value, ...partial })
  const vmCount = targetVmCount(value, tree)
  const selectedGroup = value.target_type === 'group' && value.target_id ? findGroup(tree, value.target_id) : undefined

  return <div className="grid gap-4 md:grid-cols-2">
    <Field label="Name" wide><input value={value.name} onChange={(event) => patch({ name: event.target.value })} placeholder={value.action === 'stop' ? 'Evening shutdown — Ring 1' : 'Morning start — Ring 1'} required /></Field>

    {/* First, because it changes what every field below it means. */}
    <div className="md:col-span-2 rounded-lg border border-slate-200 bg-slate-50 p-3">
      <p className="mb-2 text-sm font-medium text-slate-800">What this wave does</p>
      <ActionPicker value={value.action} onChange={(action) => patch({ action, ring_order: action === 'stop' ? 'reverse' : 'sequence' })} />
      <p className="mt-2 text-xs text-slate-600">
        {value.action === 'stop'
          ? 'Stops are gated separately from starts and skip any machine marked "never stop".'
          : 'Starts bring machines up. A machine is only ever started by its nearest start schedule.'}
      </p>
    </div>

    {value.action === 'stop' && <Field label="Stop mode" hint={STOP_MODE_HELP[value.stop_mode]}>
      <StopModePicker value={value.stop_mode} onChange={(stop_mode) => patch({ stop_mode })} />
    </Field>}
    {value.action === 'stop' && value.target_type === 'group' && <Field label="Ring order" hint="Reverse unwinds the last ring first, mirroring the start order.">
      <RingOrderPicker value={value.ring_order} onChange={(ring_order) => patch({ ring_order })} />
    </Field>}

    {!lockTarget && <Field label="Target type">
      <select value={value.target_type} onChange={(event) => patch({ target_type: event.target.value as TargetType, target_id: '' })}>
        <option value="group">Group (application or ring)</option>
        <option value="vm">Single virtual machine</option>
      </select>
    </Field>}
    {!lockTarget && <Field label="Selected target">
      <div className="flex h-[38px] items-center gap-2 rounded-lg border border-slate-300 bg-slate-50 px-3 text-sm text-slate-700">
        {value.target_type === 'group' ? <Layers size={15} className="text-blue-600" aria-hidden="true" /> : <Server size={15} className="text-slate-500" aria-hidden="true" />}
        <span className="truncate">{value.target_type === 'group' ? selectedGroup?.name_path ?? 'Choose a group below' : value.target_id ? 'Virtual machine selected' : 'Choose a virtual machine below'}</span>
      </div>
    </Field>}
    {!lockTarget && <div className="md:col-span-2">
      {value.target_type === 'group'
        ? <GroupPicker nodes={tree} value={value.target_id || null} onChange={(id) => patch({ target_id: id ?? '' })} label="Target group" />
        : <VmTargetPicker value={value.target_id} onChange={(id) => patch({ target_id: id })} />}
    </div>}

    <RecurrenceEditor
      value={{
        frequency: value.frequency,
        start_time: value.start_time,
        cron_expression: value.cron_expression,
        weekday: value.weekday,
        timezone: value.timezone,
        start_date: value.start_date,
        end_date: value.end_date,
        run_limit: value.run_limit,
      }}
      onChange={(next) => onChange({ ...value, ...next })}
    />

    <Field label="Stagger between VMs (seconds)" hint={staggerHint(vmCount, Number(value.stagger_seconds) || 0)}>
      <input type="number" autoComplete="off" min={0} max={3600} value={value.stagger_seconds} onChange={(event) => patch({ stagger_seconds: Number(event.target.value) })} />
    </Field>

    <Field label="Azure tenant" wide hint={`${value.action === 'stop' ? 'Stops' : 'Starts'} run against this connection unless a VM overrides it.`}>
      <select value={value.azure_connection_id} onChange={(event) => patch({ azure_connection_id: event.target.value })}>
        <option value="">Default connection</option>
        {connections.data?.map((item) => <option key={item.id} value={item.id}>{connectionOptionLabel(item)}</option>)}
      </select>
    </Field>

    <Field label="Notes" wide><textarea rows={2} value={value.notes} onChange={(event) => patch({ notes: event.target.value })} /></Field>

    <label className="flex items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3 md:col-span-2">
      <input className="!w-auto" type="checkbox" checked={value.enabled} onChange={(event) => patch({ enabled: event.target.checked })} />
      <span><span className="block text-sm font-medium text-slate-800">Schedule enabled</span><span className="text-xs text-slate-500">Disabled schedules are never claimed by the scheduler.</span></span>
    </label>
  </div>
}

/** Create or edit a schedule from anywhere in the product. */
export function ScheduleDrawer({ open, onClose, schedule, defaultTarget }: { open: boolean; onClose: () => void; schedule?: Schedule | null; defaultTarget?: { type: TargetType; id: string } }) {
  const client = useQueryClient()
  const tree = useGroupTree()
  const settings = useGeneralSettings()
  const [form, setForm] = useState<ScheduleFormState>(() => emptyScheduleForm('America/New_York', defaultTarget))

  const targetType = defaultTarget?.type
  const targetId = defaultTarget?.id
  useEffect(() => {
    if (!open) return
    setForm(schedule ? scheduleToForm(schedule) : emptyScheduleForm(settings.data?.default_timezone ?? 'America/New_York', targetType && targetId ? { type: targetType, id: targetId } : undefined))
  }, [open, schedule, settings.data?.default_timezone, targetType, targetId])

  const save = useMutation({
    mutationFn: () => schedule
      ? api<Schedule>(`/schedules/${schedule.id}`, json('PATCH', scheduleToPayload(form)))
      : api<Schedule>('/schedules', json('POST', scheduleToPayload(form))),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['schedules'] })
      void client.invalidateQueries({ queryKey: ['schedule'] })
      void client.invalidateQueries({ queryKey: ['group'] })
      void client.invalidateQueries({ queryKey: ['dashboard'] })
      onClose()
    },
  })

  return <Drawer
    open={open}
    onClose={onClose}
    width="max-w-3xl"
    title={schedule ? 'Edit schedule' : 'New schedule'}
    description="Schedules target a group (application or ring) or a single virtual machine."
    footer={<>
      <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
      <button type="button" className="btn-primary" disabled={save.isPending || !form.name.trim() || !form.target_id || !form.start_time} onClick={() => save.mutate()}>{save.isPending ? 'Saving…' : schedule ? 'Save changes' : 'Create schedule'}</button>
    </>}
  >
    <div className="space-y-4">
      {save.error && <ErrorNotice error={save.error} />}
      <ScheduleFields value={form} onChange={setForm} tree={tree.data ?? []} lockTarget={!!defaultTarget && !schedule} />
      {defaultTarget && !schedule && <p className="muted">Target is fixed to the group you opened this from.</p>}
    </div>
  </Drawer>
}
