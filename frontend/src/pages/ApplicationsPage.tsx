import { useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { CalendarClock, ChevronRight, Layers, MapPin, Plus, Server } from 'lucide-react'
import { api, json } from '../api'
import { useCan } from '../auth'
import { connectionLabel, useGroupTree, useScheduleIndex } from '../lib/queries'
import { useCountdown } from '../lib/time'
import { GroupEditorDrawer } from '../components/GroupForms'
import { Chip, EmptyState, ErrorNotice, PageHeader, SearchInput, Skeleton, Toggle } from '../components/Ui'
import type { Group, GroupNode, Schedule } from '../types'

function NextStart({ node, schedules }: { node: GroupNode; schedules: Schedule[] }) {
  const direct = schedules.filter((item) => item.enabled && item.next_run_at).sort((a, b) => String(a.next_run_at).localeCompare(String(b.next_run_at)))[0]
  const target = node.subtree_next_run_at ?? direct?.next_run_at ?? null
  const countdown = useCountdown(target, direct?.timezone)
  const waves = node.subtree_schedule_count ?? schedules.length
  if (!target) return <Chip tone="neutral" icon={<CalendarClock size={13} />}>No schedule</Chip>
  return <Chip tone="info" icon={<CalendarClock size={13} />} title={`Earliest of ${waves} schedule${waves === 1 ? '' : 's'} in this application`}>{countdown}</Chip>
}

function ApplicationCard({ node, schedules }: { node: GroupNode; schedules: Schedule[] }) {
  const canWrite = useCan('groups.write')
  const client = useQueryClient()
  const [optimistic, setOptimistic] = useState<boolean | null>(null)
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api<Group>(`/groups/${node.id}`, json('PATCH', { enabled })),
    onError: () => setOptimistic(null),
    onSuccess: () => { setOptimistic(null); void client.invalidateQueries({ queryKey: ['groups'] }) },
  })
  const enabled = optimistic ?? node.enabled
  return <article className="card flex flex-col gap-3">
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <Link to={`/applications/${node.id}`} className="flex items-center gap-2 text-base font-semibold text-slate-900 hover:text-blue-700">
          <Layers size={17} className="shrink-0 text-blue-600" aria-hidden="true" />
          <span className="truncate">{node.name}</span>
        </Link>
        <p className="mt-1 line-clamp-2 muted">{node.description || 'No description provided.'}</p>
      </div>
      {canWrite && <Toggle checked={enabled} busy={toggle.isPending} label={`${enabled ? 'Disable' : 'Enable'} ${node.name}`} onChange={(next) => { setOptimistic(next); toggle.mutate(next) }} />}
    </div>
    <div className="flex flex-wrap items-center gap-2">
      <Chip tone="neutral">{node.children?.length ?? 0} ring{(node.children?.length ?? 0) === 1 ? '' : 's'}</Chip>
      <Chip tone="neutral" icon={<Server size={13} />}>{node.subtree_vm_count} VM{node.subtree_vm_count === 1 ? '' : 's'}</Chip>
      <NextStart node={node} schedules={schedules} />
      <Chip tone={enabled ? 'success' : 'neutral'}>{enabled ? 'Enabled' : 'Disabled'}</Chip>
    </div>
    <p className="truncate text-xs text-slate-500">Tenant: {connectionLabel(node.effective_connection_name ?? node.connection_name, node.effective_connection_tenant_id ?? node.connection_tenant_id)}{node.connection_inherited ? ' · inherited' : ''}</p>
    <Link to={`/applications/${node.id}`} className="mt-auto inline-flex items-center gap-1 text-sm font-semibold text-blue-700 hover:text-blue-800">Open workspace<ChevronRight size={15} /></Link>
  </article>
}

/** Root applications workspace: every top-level group with its rings, VM totals and next start. */
export function ApplicationsPage() {
  const canWrite = useCan('groups.write')
  const tree = useGroupTree()
  const schedules = useScheduleIndex()
  const [query, setQuery] = useState('')
  const [state, setState] = useState('')
  const [creating, setCreating] = useState(false)

  const applications = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return (tree.data ?? [])
      .filter((item) => (needle ? `${item.name} ${item.description}`.toLowerCase().includes(needle) : true))
      .filter((item) => (state ? String(item.enabled) === state : true))
      .sort((a, b) => a.sequence - b.sequence || a.name.localeCompare(b.name))
  }, [tree.data, query, state])

  const newButton = canWrite ? <button type="button" className="btn-primary" onClick={() => setCreating(true)}><Plus size={16} />New application</button> : undefined
  const headerActions = <div className="flex flex-wrap gap-2">
    <Link to="/applications/locate" className="btn-secondary"><MapPin size={16} />Locate &amp; place VMs</Link>
    {newButton}
  </div>

  return <>
    <PageHeader title="Applications" description="Each application owns its rings, the virtual machines inside them, and their start schedules." action={headerActions} />
    <div className="mb-5 flex flex-wrap items-center gap-3">
      <SearchInput value={query} onChange={setQuery} placeholder="Search applications" label="Search applications" />
      <select className="!w-auto" value={state} onChange={(event) => setState(event.target.value)} aria-label="Filter by state">
        <option value="">Any state</option><option value="true">Enabled</option><option value="false">Disabled</option>
      </select>
    </div>
    {tree.error && <ErrorNotice error={tree.error} />}
    {tree.isLoading ? <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{Array.from({ length: 6 }).map((_, index) => <div className="card space-y-3" key={index}><Skeleton className="h-5 w-2/3" /><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-1/2" /></div>)}</div>
      : applications.length === 0 ? <EmptyState
        icon={<Layers size={22} />}
        title={query || state ? 'No applications match this view' : 'No applications yet'}
        description={query || state ? 'Clear the search and filters to see every application.' : 'Create your first application, then add rings and virtual machines beneath it.'}
        action={newButton}
      /> : <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {applications.map((node) => <ApplicationCard key={node.id} node={node} schedules={schedules.data?.byTarget.get(node.id) ?? []} />)}
      </div>}
    <GroupEditorDrawer open={creating} onClose={() => setCreating(false)} parentId={null} />
  </>
}
