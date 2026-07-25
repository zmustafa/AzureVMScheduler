import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router'
import { ArrowDown, ArrowUp, CalendarClock, ChevronRight, CornerUpRight, FolderTree, Layers, Pencil, Play, Plus, Server, Trash2 } from 'lucide-react'
import { api, json } from '../api'
import { useCan } from '../auth'
import { connectionLabel, findGroup, subtreeIds, useGroupTree, useLatestRuns, useScheduleIndex } from '../lib/queries'
import { useCountdown, zoneLabel } from '../lib/time'
import { GroupEditorDrawer, MoveGroupDrawer } from '../components/GroupForms'
import { ScheduleDrawer } from '../components/ScheduleDrawer'
import { ConfirmDialog } from '../components/Overlay'
import { StatusBadge } from '../components/StatusBadge'
import { VmTable } from '../components/VmTable'
import { Chip, EmptyState, ErrorNotice, Loading, PageHeader, Toggle } from '../components/Ui'
import type { Group, GroupDetail, GroupNode, Schedule, ScheduleRun } from '../types'

function scheduleChipText(schedule: Schedule): string {
  const cadence = schedule.schedule_type === 'daily' ? `Daily ${schedule.start_time}` : `Once ${schedule.start_time}`
  const stagger = schedule.stagger_seconds > 0 ? ` · stagger ${schedule.stagger_seconds}s` : ' · no stagger'
  return `${cadence} ${zoneLabel(schedule.timezone, schedule.next_run_at)}${stagger}`
}

function Countdown({ schedules }: { schedules: Schedule[] }) {
  const next = schedules.filter((item) => item.enabled && item.next_run_at).sort((a, b) => String(a.next_run_at).localeCompare(String(b.next_run_at)))[0]
  const text = useCountdown(next?.next_run_at ?? null, next?.timezone)
  return <Chip tone={next ? 'info' : 'neutral'} icon={<CalendarClock size={13} />} title={next ? `${next.name} (${next.timezone})` : 'No enabled schedule resolves to this node'}>{next ? text : 'No upcoming start'}</Chip>
}

function RingCard({ node, schedules, lastRun, index, total, canWrite, onReorder, onRunNow, onEditSchedule, onNewSchedule }: {
  node: GroupNode
  schedules: Schedule[]
  lastRun?: ScheduleRun
  index: number
  total: number
  canWrite: boolean
  onReorder: (from: number, to: number) => void
  onRunNow: (schedule: Schedule, node: GroupNode) => void
  onEditSchedule: (schedule: Schedule) => void
  onNewSchedule: (node: GroupNode) => void
}) {
  const client = useQueryClient()
  const [optimistic, setOptimistic] = useState<boolean | null>(null)
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api<Group>(`/groups/${node.id}`, json('PATCH', { enabled })),
    onError: () => setOptimistic(null),
    onSuccess: () => { setOptimistic(null); void client.invalidateQueries({ queryKey: ['groups'] }) },
  })
  const enabled = optimistic ?? node.enabled
  const primary = schedules.find((item) => item.enabled) ?? schedules[0]
  return <article className="card flex flex-col gap-3">
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">Sequence {index + 1}</span>
        <Link to={`/applications/${node.id}`} className="flex items-center gap-2 text-base font-semibold text-slate-900 hover:text-blue-700">
          <FolderTree size={16} className="shrink-0 text-slate-400" aria-hidden="true" />
          <span className="truncate">{node.name}</span>
        </Link>
      </div>
      {canWrite && <div className="flex shrink-0 items-center gap-1">
        <button type="button" className="btn-secondary !px-1.5 !py-1" aria-label={`Move ${node.name} earlier`} disabled={index === 0} onClick={() => onReorder(index, index - 1)}><ArrowUp size={14} /></button>
        <button type="button" className="btn-secondary !px-1.5 !py-1" aria-label={`Move ${node.name} later`} disabled={index === total - 1} onClick={() => onReorder(index, index + 1)}><ArrowDown size={14} /></button>
      </div>}
    </div>

    <div className="flex flex-wrap items-center gap-2">
      {primary
        ? <Chip tone="info" icon={<CalendarClock size={13} />}>{scheduleChipText(primary)}</Chip>
        : <Chip tone="warn" icon={<CalendarClock size={13} />}>No schedule</Chip>}
      <Chip tone="neutral" icon={<Server size={13} />}>{node.subtree_vm_count} VM{node.subtree_vm_count === 1 ? '' : 's'}</Chip>
      <Countdown schedules={schedules} />
      {lastRun ? <Chip tone={lastRun.status === 'succeeded' ? 'success' : lastRun.status === 'running' ? 'warn' : 'danger'}>Last run: {lastRun.status.replaceAll('_', ' ')}</Chip> : <Chip tone="neutral">Never run</Chip>}
      <Chip tone={enabled ? 'success' : 'neutral'}>{enabled ? 'Enabled' : 'Disabled'}</Chip>
      {(node.children?.length ?? 0) > 0 && <Chip tone="neutral">{node.children.length} nested ring{node.children.length === 1 ? '' : 's'}</Chip>}
    </div>

    <p className="truncate text-xs text-slate-500">Tenant: {connectionLabel(node.effective_connection_name ?? node.connection_name, node.effective_connection_tenant_id ?? node.connection_tenant_id)}{node.connection_inherited ? ' · inherited' : ''}</p>

    <div className="mt-auto flex flex-wrap items-center gap-2">
      <Link className="btn-secondary !py-1" to={`/applications/${node.id}`}>Open<ChevronRight size={14} /></Link>
      {canWrite && (primary
        ? <button type="button" className="btn-secondary !py-1" onClick={() => onEditSchedule(primary)}><Pencil size={14} />Edit schedule</button>
        : <button type="button" className="btn-secondary !py-1" onClick={() => onNewSchedule(node)}><Plus size={14} />Add schedule</button>)}
      {canWrite && primary && <button type="button" className="btn-secondary !py-1" onClick={() => onRunNow(primary, node)}><Play size={14} />Run now</button>}
      {canWrite && <Toggle checked={enabled} busy={toggle.isPending} label={`${enabled ? 'Disable' : 'Enable'} ${node.name}`} onChange={(next) => { setOptimistic(next); toggle.mutate(next) }} />}
    </div>
  </article>
}

/** Group workspace: breadcrumb header, the ring board in sequence order, and the group's own VM inventory. */
export function GroupDetailPage() {
  const { groupId } = useParams()
  const navigate = useNavigate()
  const client = useQueryClient()
  const canWriteGroups = useCan('groups.write')
  const canRunSchedules = useCan('schedules.write')

  const detail = useQuery({ queryKey: ['group', groupId], queryFn: () => api<GroupDetail>(`/groups/${groupId}`), enabled: !!groupId })
  const tree = useGroupTree()
  const schedules = useScheduleIndex()
  const runs = useLatestRuns()

  const [editing, setEditing] = useState(false)
  const [creatingRing, setCreatingRing] = useState(false)
  const [moving, setMoving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [scheduleTarget, setScheduleTarget] = useState<{ schedule?: Schedule; node?: GroupNode } | null>(null)
  const [runTarget, setRunTarget] = useState<{ schedule: Schedule; node: GroupNode | Group; vmCount: number } | null>(null)

  const node = useMemo(() => (groupId ? findGroup(tree.data ?? [], groupId) : undefined), [tree.data, groupId])
  const children = useMemo(() => [...(node?.children ?? [])].sort((a, b) => a.sequence - b.sequence || a.name.localeCompare(b.name)), [node])

  const subtreeSchedules = useMemo(() => {
    if (!node || !schedules.data) return []
    const ids = subtreeIds(node)
    return schedules.data.items.filter((item) => item.target_type === 'group' && ids.has(item.target_id))
  }, [node, schedules.data])

  const ownSchedules = detail.data?.schedules ?? []

  const invalidateAll = () => {
    void client.invalidateQueries({ queryKey: ['groups'] })
    void client.invalidateQueries({ queryKey: ['group'] })
    void client.invalidateQueries({ queryKey: ['schedules'] })
    void client.invalidateQueries({ queryKey: ['runs'] })
  }

  const toggleGroup = useMutation({ mutationFn: (enabled: boolean) => api<Group>(`/groups/${groupId}`, json('PATCH', { enabled })), onSuccess: invalidateAll })
  const reorder = useMutation({ mutationFn: (orderedIds: string[]) => api('/groups/reorder', json('POST', { parent_id: groupId, ordered_ids: orderedIds })), onSuccess: invalidateAll })
  const remove = useMutation({ mutationFn: () => api(`/groups/${groupId}`, json('DELETE')), onSuccess: () => { invalidateAll(); navigate('/applications') } })
  const runNow = useMutation({ mutationFn: (scheduleId: string) => api<ScheduleRun | null>(`/schedules/${scheduleId}/run`, json('POST')), onSuccess: () => { invalidateAll(); setRunTarget(null) } })

  if (detail.isLoading || tree.isLoading) return <Loading />
  if (detail.error || !detail.data) return <ErrorNotice error={detail.error ?? new Error('Group not found')} />

  const group = detail.data.group
  const ancestors = detail.data.ancestors
  const isApplication = group.depth === 0
  const ownVmCount = detail.data.vms.length
  const subtreeVmCount = node?.subtree_vm_count ?? ownVmCount
  const primarySchedule = ownSchedules.find((item) => item.enabled) ?? ownSchedules[0]

  const handleReorder = (from: number, to: number) => {
    const ordered = children.map((item) => item.id)
    const [moved] = ordered.splice(from, 1)
    ordered.splice(to, 0, moved)
    reorder.mutate(ordered)
  }

  return <>
    <nav aria-label="Breadcrumb" className="mb-3 flex flex-wrap items-center gap-1 text-sm text-slate-600">
      <Link className="link" to="/applications">Applications</Link>
      {ancestors.map((item) => <span key={item.id} className="flex items-center gap-1"><ChevronRight size={14} aria-hidden="true" /><Link className="link" to={`/applications/${item.id}`}>{item.name}</Link></span>)}
      <span className="flex items-center gap-1"><ChevronRight size={14} aria-hidden="true" /><span className="font-semibold text-slate-900">{group.name}</span></span>
    </nav>

    <PageHeader
      title={group.name}
      description={group.description || (isApplication ? 'Application root — rings run in sequence order.' : 'Ring inside an application.')}
      action={<div className="flex flex-wrap items-center gap-2">
        {canWriteGroups && <Toggle checked={group.enabled} busy={toggleGroup.isPending} label={`${group.enabled ? 'Disable' : 'Enable'} ${group.name}`} onChange={(next) => toggleGroup.mutate(next)} />}
        {canRunSchedules && primarySchedule && <button type="button" className="btn-primary" onClick={() => setRunTarget({ schedule: primarySchedule, node: group, vmCount: subtreeVmCount })}><Play size={15} />Run now</button>}
        {canWriteGroups && <button type="button" className="btn-secondary" onClick={() => setEditing(true)}><Pencil size={15} />Edit</button>}
        {canWriteGroups && <button type="button" className="btn-secondary" onClick={() => setMoving(true)}><CornerUpRight size={15} />Move</button>}
        {canWriteGroups && <button type="button" className="btn-danger" onClick={() => setDeleting(true)}><Trash2 size={15} />Delete</button>}
      </div>}
    />

    {(runNow.error || reorder.error || toggleGroup.error) && <div className="mb-4"><ErrorNotice error={runNow.error ?? reorder.error ?? toggleGroup.error} /></div>}

    <section className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <div className="card"><p className="muted">Aggregate next start</p><div className="mt-2"><Countdown schedules={subtreeSchedules} /></div></div>
      <div className="card"><p className="muted">Virtual machines</p><p className="mt-2 text-2xl font-semibold text-slate-900">{subtreeVmCount}</p><p className="text-xs text-slate-500">{ownVmCount} directly in this {group.kind}</p></div>
      <div className="card"><p className="muted">Rings</p><p className="mt-2 text-2xl font-semibold text-slate-900">{isApplication ? children.length : '—'}</p><p className="text-xs text-slate-500">{isApplication ? 'Directly in this application' : 'Rings hold virtual machines, not other rings'}</p></div>
      <div className="card"><p className="muted">Tenant</p><p className="mt-2 truncate text-sm font-medium text-slate-800">{connectionLabel(group.effective_connection_name ?? group.connection_name, group.effective_connection_tenant_id ?? group.connection_tenant_id)}</p><div className="mt-2 flex gap-2">{group.connection_inherited && <Chip tone="neutral">Inherited</Chip>}<Chip tone={group.enabled ? 'success' : 'neutral'}>{group.enabled ? 'Enabled' : 'Disabled'}</Chip>{!group.effective_enabled && <Chip tone="warn">Ancestor disabled</Chip>}</div></div>
    </section>

    {isApplication && <section className="mb-8 space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div><h2 className="text-lg font-semibold text-slate-900">Ring board</h2><p className="muted">Rings execute in sequence order and hold the virtual machines.</p></div>
        {canWriteGroups && <button type="button" className="btn-primary" onClick={() => setCreatingRing(true)}><Plus size={16} />New ring</button>}
      </div>
      {children.length === 0 ? <EmptyState
        icon={<Layers size={22} />}
        title="No rings yet"
        description="Add a ring to stage starts, or keep the virtual machines directly on the application."
        action={canWriteGroups ? <button type="button" className="btn-primary" onClick={() => setCreatingRing(true)}><Plus size={16} />New ring</button> : undefined}
      /> : <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {children.map((child, index) => {
          const childSchedules = schedules.data?.byTarget.get(child.id) ?? []
          const primary = childSchedules.find((item) => item.enabled) ?? childSchedules[0]
          return <RingCard
            key={child.id}
            node={child}
            schedules={childSchedules}
            lastRun={primary ? runs.data?.get(primary.id) : undefined}
            index={index}
            total={children.length}
            canWrite={canWriteGroups}
            onReorder={handleReorder}
            onRunNow={(schedule, target) => setRunTarget({ schedule, node: target, vmCount: target.subtree_vm_count })}
            onEditSchedule={(schedule) => setScheduleTarget({ schedule })}
            onNewSchedule={(target) => setScheduleTarget({ node: target })}
          />
        })}
      </div>}
    </section>}

    <section className="mb-8 space-y-3">
      <h2 className="text-lg font-semibold text-slate-900">Schedules on this {group.kind}</h2>
      {ownSchedules.length === 0 ? <p className="muted">No schedule targets this node directly. Virtual machines inherit the nearest ancestor schedule.</p> : <ul className="grid gap-3 md:grid-cols-2">
        {ownSchedules.map((schedule) => <li key={schedule.id} className="card flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <Link className="font-medium text-slate-900 hover:text-blue-700" to={`/schedules/${schedule.id}`}>{schedule.name}</Link>
            <p className="mt-1 text-xs text-slate-500">{scheduleChipText(schedule)}</p>
          </div>
          <div className="flex items-center gap-2"><StatusBadge value={schedule.status} /><Countdown schedules={[schedule]} /></div>
        </li>)}
      </ul>}
      {canRunSchedules && <button type="button" className="btn-secondary" onClick={() => setScheduleTarget({ node: node ?? undefined })}><Plus size={15} />New schedule for this {group.kind}</button>}
    </section>

    <VmTable groupId={group.id} groupName={group.name} canHaveRings={isApplication} title={`Virtual machines in ${group.name}`} description={isApplication ? 'Direct members by default; include rings to see every VM in the application.' : 'Every virtual machine in this ring.'} />

    <GroupEditorDrawer open={editing} onClose={() => setEditing(false)} group={group} />
    <GroupEditorDrawer open={creatingRing} onClose={() => setCreatingRing(false)} parentId={group.id} parentName={group.name_path} />
    <MoveGroupDrawer open={moving} onClose={() => setMoving(false)} group={node ?? null} tree={tree.data ?? []} />
    <ScheduleDrawer
      open={scheduleTarget !== null}
      onClose={() => setScheduleTarget(null)}
      schedule={scheduleTarget?.schedule ?? null}
      defaultTarget={scheduleTarget?.schedule ? undefined : scheduleTarget?.node ? { type: 'group', id: scheduleTarget.node.id } : undefined}
    />
    <ConfirmDialog open={deleting} title={`Delete ${group.name}`} confirmLabel="Delete group" busy={remove.isPending} onCancel={() => setDeleting(false)} onConfirm={() => remove.mutate()}>
      <p>This removes <strong>{group.name_path}</strong>, every ring beneath it, <strong>{subtreeVmCount}</strong> virtual machine{subtreeVmCount === 1 ? '' : 's'} and their schedules.</p>
      <p className="mt-2">Tenant: <strong>{connectionLabel(group.connection_name, group.connection_tenant_id)}</strong>. This cannot be undone.</p>
    </ConfirmDialog>
    <ConfirmDialog open={runTarget !== null} tone="primary" title="Start virtual machines now" confirmLabel="Run now" busy={runNow.isPending} onCancel={() => setRunTarget(null)} onConfirm={() => runTarget && runNow.mutate(runTarget.schedule.id)}>
      <p>Schedule <strong>{runTarget?.schedule.name}</strong> will start <strong>{runTarget?.vmCount ?? 0}</strong> virtual machine{(runTarget?.vmCount ?? 0) === 1 ? '' : 's'} in <strong>{runTarget?.node.name_path}</strong>.</p>
      <p className="mt-2">Tenant: <strong>{connectionLabel(runTarget?.schedule.connection_name, runTarget?.schedule.connection_tenant_id)}</strong>.</p>
    </ConfirmDialog>
  </>
}
