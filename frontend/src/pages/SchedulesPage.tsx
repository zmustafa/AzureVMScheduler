import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { CalendarClock, Layers, Plus, Server } from 'lucide-react'
import { api } from '../api'
import { useCan } from '../auth'
import { connectionLabel, connectionOptionLabel, findGroup, useConnections, useGroupTree } from '../lib/queries'
import { useCountdown, useDisplayTimezone, zoneLabel } from '../lib/time'
import { recurrenceBounds, recurrenceSummary } from '../lib/recurrence'
import { useSort } from '../lib/sorting'
import { ScheduleDrawer } from '../components/ScheduleDrawer'
import { ActionBadge } from '../components/ActionBits'
import { SortHeader } from '../components/SortHeader'
import { StatusBadge } from '../components/StatusBadge'
import { Chip, EmptyState, ErrorNotice, Pagination, PageHeader, SearchInput, TableSkeleton } from '../components/Ui'
import type { Paged, Schedule } from '../types'

const LIMIT = 50

function NextRunCell({ schedule }: { schedule: Schedule }) {
  const { format } = useDisplayTimezone()
  const countdown = useCountdown(schedule.next_run_at, schedule.timezone)
  return <div>
    <span className="block text-slate-800">{format(schedule.next_run_at, schedule.timezone)}</span>
    <span className="text-xs font-medium text-blue-700">{schedule.next_run_at ? countdown : 'Not scheduled'}</span>
  </div>
}

/** Schedule list for the group/VM targeting model. */
export function SchedulesPage() {
  const canWrite = useCan('schedules.write')
  const canReadGroups = useCan('groups.read')
  const canReadVms = useCan('vms.read')
  const connections = useConnections()
  const tree = useGroupTree(canReadGroups)
  const [query, setQuery] = useState('')
  const [action, setAction] = useState('')
  const [targetType, setTargetType] = useState('')
  const [status, setStatus] = useState('')
  const [enabled, setEnabled] = useState('')
  const [connectionId, setConnectionId] = useState('')
  const [offset, setOffset] = useState(0)
  const [creating, setCreating] = useState(false)
  const { sort, toggle, params: sortParams } = useSort({ key: 'next_run_at', direction: 'asc' })

  useEffect(() => { setOffset(0) }, [query, status, enabled, connectionId, targetType, action, sort])

  const path = useMemo(() => {
    const params = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) })
    if (query.trim()) params.set('q', query.trim())
    if (status) params.set('status', status)
    if (enabled) params.set('enabled', enabled)
    if (connectionId) params.set('connection_id', connectionId)
    if (action) params.set('action', action)
    return `/schedules?${params.toString()}&${sortParams}`
  }, [query, status, enabled, connectionId, action, offset, sortParams])

  const list = useQuery({ queryKey: ['schedules', path], queryFn: () => api<Paged<Schedule>>(path) })
  const rows = useMemo(() => (list.data?.items ?? []).filter((item) => (targetType ? item.target_type === targetType : true)), [list.data, targetType])

  const vmCountFor = (schedule: Schedule) => schedule.vm_count ?? (schedule.target_type === 'vm' ? 1 : findGroup(tree.data ?? [], schedule.target_id)?.subtree_vm_count ?? 0)
  const canCreate = canWrite && canReadGroups && canReadVms
  const newButton = canCreate ? <button type="button" className="btn-primary" onClick={() => setCreating(true)}><Plus size={16} />New schedule</button> : undefined

  return <>
    <PageHeader title="Schedules" description="Daily, weekly or cron waves that start or stop a group (application or ring) or a single virtual machine." action={newButton} />

    <div className="mb-4 flex flex-wrap items-center gap-3">
      <SearchInput value={query} onChange={setQuery} placeholder="Search schedules" label="Search schedules" />
      <select className="!w-auto" value={action} onChange={(event) => setAction(event.target.value)} aria-label="Filter by action">
        <option value="">Any action</option><option value="start">Starts</option><option value="stop">Stops</option>
      </select>
      <select className="!w-auto" value={targetType} onChange={(event) => setTargetType(event.target.value)} aria-label="Filter by target type">
        <option value="">Any target</option><option value="group">Groups</option><option value="vm">Virtual machines</option>
      </select>
      <select className="!w-auto" value={status} onChange={(event) => setStatus(event.target.value)} aria-label="Filter by status">
        <option value="">Any status</option><option value="scheduled">Scheduled</option><option value="disabled">Disabled</option><option value="running">Running</option><option value="failed">Failed</option><option value="completed">Completed</option>
      </select>
      <select className="!w-auto" value={enabled} onChange={(event) => setEnabled(event.target.value)} aria-label="Filter by enabled state">
        <option value="">Any state</option><option value="true">Enabled</option><option value="false">Disabled</option>
      </select>
      <select className="!w-auto" value={connectionId} onChange={(event) => setConnectionId(event.target.value)} aria-label="Filter by Azure tenant">
        <option value="">Any tenant</option>
        {connections.data?.map((item) => <option key={item.id} value={item.id}>{connectionOptionLabel(item)}</option>)}
      </select>
    </div>

    {list.error && <div className="mb-4"><ErrorNotice error={list.error} /></div>}

    {list.isLoading ? <TableSkeleton columns={6} /> : rows.length === 0 ? <EmptyState
      icon={<CalendarClock size={22} />}
      title="No schedules match this view"
      description="Create a schedule against an application, a ring, or a single virtual machine."
      action={newButton}
    /> : <>
      <div className="surface hidden max-h-[70vh] overflow-auto md:block">
        <table className="u-table">
          <thead><tr>
            <SortHeader label="Name" sortKey="name" sort={sort} onSort={toggle} />
            <SortHeader label="Action" sortKey="action" sort={sort} onSort={toggle} />
            <SortHeader label="Target" />
            <SortHeader label="Type" sortKey="schedule_type" sort={sort} onSort={toggle} />
            <SortHeader label="Recurrence" />
            <SortHeader label="Next run" sortKey="next_run_at" sort={sort} onSort={toggle} />
            <SortHeader label="VMs" />
            <SortHeader label="Connection" />
            <SortHeader label="Status" sortKey="status" sort={sort} onSort={toggle} />
          </tr></thead>
          <tbody>{rows.map((schedule) => <tr key={schedule.id}>
            <td><Link className="font-medium text-slate-900 hover:text-blue-700" to={`/schedules/${schedule.id}`}>{schedule.name}</Link></td>
            <td><ActionBadge action={schedule.action} stopMode={schedule.stop_mode} /></td>
            <td>
              <span className="flex items-center gap-2">
                {schedule.target_type === 'group' ? <Layers size={14} className="shrink-0 text-blue-600" aria-hidden="true" /> : <Server size={14} className="shrink-0 text-slate-500" aria-hidden="true" />}
                <span className="truncate">{schedule.target_label ?? schedule.target_id}</span>
              </span>
              <span className="text-xs uppercase tracking-wide text-slate-400">{schedule.target_type}</span>
            </td>
            <td className="capitalize">{schedule.schedule_type.replace('_', ' ')}</td>
            <td>
              <span className={schedule.schedule_type === 'cron' ? 'font-mono text-xs' : ''}>{recurrenceSummary(schedule)}</span>
              <span className="block text-xs text-slate-500">{schedule.timezone} ({zoneLabel(schedule.timezone, schedule.next_run_at)})</span>
              {recurrenceBounds(schedule) && <span className="block text-xs text-slate-400">{recurrenceBounds(schedule)}</span>}
            </td>
            <td><NextRunCell schedule={schedule} /></td>
            <td>{vmCountFor(schedule)}</td>
            <td>{connectionLabel(schedule.connection_name, schedule.connection_tenant_id)}</td>
            <td><StatusBadge value={schedule.status} /></td>
          </tr>)}</tbody>
        </table>
      </div>

      <ul className="space-y-3 md:hidden">{rows.map((schedule) => <li key={schedule.id} className="card space-y-2">
        <div className="flex items-start justify-between gap-3">
          <Link className="font-medium text-slate-900" to={`/schedules/${schedule.id}`}>{schedule.name}</Link>
          <StatusBadge value={schedule.status} />
        </div>
        <ActionBadge action={schedule.action} stopMode={schedule.stop_mode} />
        <p className="flex items-center gap-2 text-xs text-slate-600">{schedule.target_type === 'group' ? <Layers size={13} aria-hidden="true" /> : <Server size={13} aria-hidden="true" />}{schedule.target_label ?? schedule.target_id}</p>
        <p className="text-xs text-slate-600">{recurrenceSummary(schedule)} · {zoneLabel(schedule.timezone, schedule.next_run_at)}</p>
        <NextRunCell schedule={schedule} />
        <Chip tone="neutral">{connectionLabel(schedule.connection_name, schedule.connection_tenant_id)}</Chip>
      </li>)}</ul>

      <div className="surface mt-3"><Pagination total={list.data?.total ?? 0} limit={LIMIT} offset={offset} onChange={setOffset} /></div>
    </>}

    {canCreate && <ScheduleDrawer open={creating} onClose={() => setCreating(false)} />}
  </>
}
