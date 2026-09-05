import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { Activity, Download, ExternalLink, Pencil, Play, Plus, Server, Square, Trash2 } from 'lucide-react'
import { api, json } from '../api'
import { useCan } from '../auth'
import { azurePortalUrl, connectionLabel, connectionOptionLabel, useConnections, useGroupTree } from '../lib/queries'
import { useDisplayTimezone } from '../lib/time'
import { useSort } from '../lib/sorting'
import { AddVmsDrawer } from './AddVmsDrawer'
import { ProtectedChip } from './ActionBits'
import { GroupPicker } from './GroupPicker'
import { ConfirmDialog, Drawer } from './Overlay'
import { PowerActionDialog } from './PowerActionDialog'
import { SortHeader } from './SortHeader'
import { Chip, EmptyState, ErrorNotice, Field, Pagination, SearchInput, TableSkeleton, Toggle } from './Ui'
import type { Paged, PowerStateResult, PowerStateScan, VirtualMachine } from '../types'

const LIMIT = 50
type BulkAction = 'move' | 'enable' | 'disable' | 'delete'

/** Azure reports power state as running/stopped/deallocated/starting/…; anything else is shown verbatim. */
function powerTone(result: PowerStateResult): 'success' | 'neutral' | 'warn' | 'danger' {
  if (result.status === 'error') return 'danger'
  if (result.status === 'not_found') return 'warn'
  if (result.power_state === 'running') return 'success'
  if (result.power_state === 'starting') return 'warn'
  return 'neutral'
}

/** The value the power filter matches on: the reported state, or why it could not be read. */
function powerKey(result?: PowerStateResult): string {
  if (!result) return 'unscanned'
  if (result.status === 'ok') return result.power_state ?? 'unknown'
  return result.status
}

const POWER_LABELS: Record<string, string> = { unscanned: 'Not scanned', not_found: 'Not in Azure', error: 'Scan failed' }
const powerLabel = (key: string) => POWER_LABELS[key] ?? key

function PowerCell({ result }: { result?: PowerStateResult }) {
  if (!result) return <span className="text-xs text-slate-400">Not scanned</span>
  const label = result.status === 'error' ? 'Scan failed' : result.status === 'not_found' ? 'Not in Azure' : result.power_state ?? 'unknown'
  return <Chip tone={powerTone(result)} title={result.message || undefined}>{label}</Chip>
}

function PortalLink({ vm, compact }: { vm: VirtualMachine; compact?: boolean }) {
  const name = vm.display_name || vm.vm_name
  return <a
    className={`inline-flex shrink-0 items-center gap-1 text-slate-500 transition hover:text-blue-700 ${compact ? '' : 'text-xs'}`}
    href={azurePortalUrl(vm.vm_resource_id, vm.effective_connection_tenant_id ?? vm.connection_tenant_id)}
    target="_blank"
    rel="noopener noreferrer"
    title={`Open ${name} in the Azure portal`}
    aria-label={`Open ${name} in the Azure portal (opens in a new tab)`}
  ><ExternalLink size={13} aria-hidden="true" />{compact ? 'Azure portal' : null}</a>
}

function useVmMutations(onDone: () => void) {
  const client = useQueryClient()
  const invalidate = () => {
    void client.invalidateQueries({ queryKey: ['vms'] })
    void client.invalidateQueries({ queryKey: ['groups'] })
    void client.invalidateQueries({ queryKey: ['group'] })
    onDone()
  }
  const patch = useMutation({ mutationFn: (input: { id: string; body: Record<string, unknown> }) => api<VirtualMachine>(`/vms/${input.id}`, json('PATCH', input.body)), onSuccess: invalidate })
  const remove = useMutation({ mutationFn: (id: string) => api(`/vms/${id}`, json('DELETE')), onSuccess: invalidate })
  const bulk = useMutation({ mutationFn: (input: { vm_ids: string[]; action: BulkAction; group_id?: string | null }) => api<{ affected: number }>('/vms/bulk', json('POST', input)), onSuccess: invalidate })
  return { patch, remove, bulk }
}

function VmEditDrawer({ vm, onClose, canReadGroups, canDelete }: { vm: VirtualMachine | null; onClose: () => void; canReadGroups: boolean; canDelete: boolean }) {
  const connections = useConnections()
  const tree = useGroupTree(canReadGroups)
  const { patch, remove } = useVmMutations(onClose)
  const [displayName, setDisplayName] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [neverStop, setNeverStop] = useState(false)
  const [connectionId, setConnectionId] = useState('')
  const [notes, setNotes] = useState('')
  const [groupId, setGroupId] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  useEffect(() => {
    if (!vm) return
    setDisplayName(vm.display_name)
    setEnabled(vm.enabled)
    setNeverStop(vm.never_stop)
    setConnectionId(vm.azure_connection_id ?? '')
    setNotes(vm.notes)
    setGroupId(vm.group_id)
  }, [vm])

  if (!vm) return null
  return <>
    <Drawer
      open
      onClose={onClose}
      title={vm.display_name || vm.vm_name}
      description={vm.vm_resource_id}
      footer={<>
        {canDelete && <button type="button" className="btn-danger" onClick={() => setConfirmDelete(true)}><Trash2 size={15} />Delete</button>}
        <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
        <button type="button" className="btn-primary" disabled={patch.isPending} onClick={() => patch.mutate({ id: vm.id, body: { display_name: displayName, enabled, never_stop: neverStop, azure_connection_id: connectionId || null, notes, group_id: groupId ?? vm.group_id } })}>{patch.isPending ? 'Saving…' : 'Save changes'}</button>
      </>}
    >
      <div className="space-y-4">
        {patch.error && <ErrorNotice error={patch.error} />}
        <Field label="Display name"><input value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></Field>
        <Field label="Azure tenant override" hint={`Effective today: ${connectionLabel(vm.effective_connection_name, vm.connection_tenant_id)}`}>
          <select value={connectionId} onChange={(event) => setConnectionId(event.target.value)}>
            <option value="">Inherit from group / default</option>
            {connections.data?.map((item) => <option key={item.id} value={item.id}>{connectionOptionLabel(item)}</option>)}
          </select>
        </Field>
        <Field label="Notes"><textarea rows={3} value={notes} onChange={(event) => setNotes(event.target.value)} /></Field>
        {canReadGroups && <Field label="Group" hint={`Currently in ${vm.group_path}`}>
          <GroupPicker nodes={tree.data ?? []} value={groupId} onChange={setGroupId} label="Group" />
        </Field>}
        <label className="flex items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
          <input className="!w-auto" type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
          <span><span className="block text-sm font-medium text-slate-800">Enabled</span><span className="text-xs text-slate-500">Disabled VMs are skipped by every schedule.</span></span>
        </label>
        <label className="flex items-center gap-3 rounded-lg border border-sky-200 bg-sky-50 p-3">
          <input className="!w-auto" type="checkbox" checked={neverStop} onChange={(event) => setNeverStop(event.target.checked)} />
          <span>
            <span className="block text-sm font-medium text-slate-800">Never stop</span>
            <span className="text-xs text-slate-600">Stop waves and on-demand stops can never touch this machine. Starts are unaffected.</span>
            {!neverStop && vm.stop_protected && <span className="mt-1 block text-xs font-medium text-sky-800">Already protected by a group above this machine.</span>}
          </span>
        </label>
      </div>
    </Drawer>
    <ConfirmDialog open={confirmDelete} title="Delete virtual machine" confirmLabel="Delete VM" busy={remove.isPending} onCancel={() => setConfirmDelete(false)} onConfirm={() => remove.mutate(vm.id)}>
      <p><strong>{vm.display_name || vm.vm_name}</strong> in <strong>{vm.group_path}</strong> will be removed from the inventory, along with any schedule that targets it directly.</p>
      <p className="mt-2">Tenant: {connectionLabel(vm.effective_connection_name, vm.connection_tenant_id)}</p>
    </ConfirmDialog>
  </>
}

function BulkMoveDrawer({ open, count, onClose, onMove, busy, canReadGroups }: { open: boolean; count: number; onClose: () => void; onMove: (groupId: string) => void; busy: boolean; canReadGroups: boolean }) {
  const tree = useGroupTree(canReadGroups)
  const [groupId, setGroupId] = useState<string | null>(null)
  useEffect(() => { if (!open) setGroupId(null) }, [open])
  return <Drawer
    open={open}
    onClose={onClose}
    title={`Move ${count} virtual machine${count === 1 ? '' : 's'}`}
    description="Pick the destination application or ring."
    footer={<>
      <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
      <button type="button" className="btn-primary" disabled={!groupId || busy} onClick={() => groupId && onMove(groupId)}>{busy ? 'Moving…' : 'Move'}</button>
    </>}
  >
    <GroupPicker nodes={tree.data ?? []} value={groupId} onChange={setGroupId} label="Destination group" />
  </Drawer>
}

type Props = { groupId?: string; groupName?: string; title?: string; description?: string; canHaveRings?: boolean }

/** Virtual machine inventory table with server-side paging, filters, bulk actions and inline editing. */
export function VmTable({ groupId, groupName, title = 'Virtual machines', description = 'Inventory resolved from the group hierarchy.', canHaveRings = true }: Props) {
  const canWrite = useCan('vms.write')
  const canReadGroups = useCan('groups.read')
  // Running a wave by hand is a scheduling action, not an inventory edit.
  const canRunWaves = useCan('schedules.write')
  const connections = useConnections()
  const tree = useGroupTree(canReadGroups)
  const [query, setQuery] = useState('')
  const [enabledFilter, setEnabledFilter] = useState('')
  const [connectionFilter, setConnectionFilter] = useState('')
  const [groupFilter, setGroupFilter] = useState('')
  const [includeRings, setIncludeRings] = useState(false)
  const [powerFilter, setPowerFilter] = useState('')
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [editing, setEditing] = useState<VirtualMachine | null>(null)
  const [adding, setAdding] = useState(false)
  const [moving, setMoving] = useState(false)
  const [confirm, setConfirm] = useState<BulkAction | null>(null)
  const [powerAction, setPowerAction] = useState<'start' | 'stop' | null>(null)
  const [pending, setPending] = useState<Record<string, boolean>>({})
  const [power, setPower] = useState<Record<string, PowerStateResult>>({})
  const [scan, setScan] = useState<{ checked_at: string; scanned: number; failed: number } | null>(null)
  const { format } = useDisplayTimezone()
  const { sort, toggle, params: sortParams } = useSort({ key: 'vm_name', direction: 'asc' })

  useEffect(() => { setOffset(0); setSelected(new Set()) }, [query, enabledFilter, connectionFilter, groupFilter, includeRings, groupId, sort])
  useEffect(() => { setSelected(new Set()) }, [offset, powerFilter])

  /** Filters shared by the table query and the CSV export, so the export matches what you see. */
  const filterParams = useMemo(() => {
    const params = new URLSearchParams()
    if (query.trim()) params.set('q', query.trim())
    if (enabledFilter) params.set('enabled', enabledFilter)
    if (groupId) { params.set('group_id', groupId); params.set('recursive', String(includeRings)) }
    else {
      if (connectionFilter) params.set('connection_id', connectionFilter)
      if (groupFilter) params.set('group_id', groupFilter)
    }
    return params
  }, [query, enabledFilter, connectionFilter, groupFilter, groupId, includeRings])

  const path = useMemo(() => {
    if (groupId) {
      const params = new URLSearchParams({ recursive: String(includeRings), limit: String(LIMIT), offset: String(offset) })
      if (query.trim()) params.set('q', query.trim())
      if (enabledFilter) params.set('enabled', enabledFilter)
      return `/groups/${groupId}/vms?${params.toString()}&${sortParams}`
    }
    const params = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) })
    if (query.trim()) params.set('q', query.trim())
    if (enabledFilter) params.set('enabled', enabledFilter)
    if (connectionFilter) params.set('connection_id', connectionFilter)
    if (groupFilter) params.set('group_id', groupFilter)
    return `/vms?${params.toString()}&${sortParams}`
  }, [groupId, includeRings, offset, query, enabledFilter, connectionFilter, groupFilter, sortParams])

  const exportHref = `/api/vms/export.csv?${new URLSearchParams([...filterParams, ['sort', sort.key], ['direction', sort.direction]]).toString()}`

  // Only the current page is ever scanned, so the power filter cannot outlive it.
  useEffect(() => { setPowerFilter('') }, [path])

  const list = useQuery({ queryKey: ['vms', path], queryFn: () => api<Paged<VirtualMachine>>(path) })
  const { patch, bulk } = useVmMutations(() => { setSelected(new Set()); setEditing(null); setMoving(false); setConfirm(null) })

  const powerScan = useMutation({
    mutationFn: (vmIds: string[]) => api<PowerStateScan>('/vms/power-state', json('POST', { vm_ids: vmIds })),
    onSuccess: (result) => {
      setPower((current) => ({ ...current, ...Object.fromEntries(result.items.map((item) => [item.vm_id, item])) }))
      setScan({ checked_at: result.checked_at, scanned: result.scanned, failed: result.failed })
    },
  })

  /** Rows the server returned for this page, before the client-side power filter. */
  const pageRows = useMemo(() => {
    return list.data?.items ?? []
  }, [list.data])

  // Power state is live, not stored, so it can only narrow the rows already scanned on this page.
  const rows = useMemo(
    () => (powerFilter ? pageRows.filter((item) => powerKey(power[item.id]) === powerFilter) : pageRows),
    [pageRows, powerFilter, power],
  )

  const powerOptions = useMemo(() => {
    const seen = new Set(pageRows.map((item) => powerKey(power[item.id])))
    return [...seen].sort((a, b) => (a === 'unscanned' ? 1 : b === 'unscanned' ? -1 : a.localeCompare(b)))
  }, [pageRows, power])

  const toggleEnabled = (vm: VirtualMachine, next: boolean) => {
    setPending((current) => ({ ...current, [vm.id]: true }))
    patch.mutate({ id: vm.id, body: { enabled: next } }, { onSettled: () => setPending((current) => ({ ...current, [vm.id]: false })) })
  }

  const allSelected = rows.length > 0 && rows.every((item) => selected.has(item.id))
  const selectedVms = rows.filter((item) => selected.has(item.id))
  const tenants = new Set(selectedVms.map((item) => connectionLabel(item.effective_connection_name, item.connection_tenant_id)))

  const cell = (vm: VirtualMachine) => <>
    <td>
      <div className="flex items-center gap-2">
        <span className="font-medium text-slate-900">{vm.display_name || vm.vm_name}</span>
        <PortalLink vm={vm} />
        {vm.azure_connection_id && <Chip tone="accent" title="This VM overrides the inherited tenant">Tenant override</Chip>}
        {vm.stop_protected && <ProtectedChip inherited={!vm.never_stop} />}
        {vm.notes && <Chip tone="neutral" title={vm.notes}>Note</Chip>}
      </div>
      <span className="block text-xs text-slate-500">{vm.group_path}</span>
    </td>
    <td className="font-mono text-xs">{vm.vm_name}</td>
    <td>{vm.resource_group}</td>
    <td className="font-mono text-xs">{vm.subscription_id}</td>
    <td>{connectionLabel(vm.effective_connection_name, vm.effective_connection_tenant_id ?? vm.connection_tenant_id)}</td>
    <td><PowerCell result={power[vm.id]} /></td>
    <td>
      <div className="flex items-center gap-2">
        <Toggle checked={vm.enabled} busy={pending[vm.id]} disabled={!canWrite} label={`${vm.enabled ? 'Disable' : 'Enable'} ${vm.display_name || vm.vm_name}`} onChange={(next) => toggleEnabled(vm, next)} />
        <Chip tone={vm.enabled ? 'success' : 'neutral'}>{vm.enabled ? 'Enabled' : 'Disabled'}</Chip>
      </div>
    </td>
    <td className="text-right">{canWrite && <button type="button" className="btn-secondary !px-2 !py-1" aria-label={`Edit ${vm.display_name || vm.vm_name}`} onClick={() => setEditing(vm)}><Pencil size={14} /></button>}</td>
  </>

  return <section className="space-y-4">
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div><h2 className="text-lg font-semibold text-slate-900">{title}</h2><p className="muted">{description}</p></div>
      <div className="flex flex-wrap items-center gap-2">
        <a
          className="btn-secondary"
          href={exportHref}
          download
          title="Download every virtual machine matching the current filters, in the CSV import format"
        ><Download size={16} />Export CSV</a>
        <button
          type="button"
          className="btn-secondary"
          disabled={pageRows.length === 0 || powerScan.isPending}
          title="Read the live power state of the virtual machines listed below"
          onClick={() => powerScan.mutate(pageRows.map((item) => item.id))}
        ><Activity size={16} />{powerScan.isPending ? 'Scanning…' : 'Scan power state'}</button>
        {canWrite && groupId && <button type="button" className="btn-primary" onClick={() => setAdding(true)}><Plus size={16} />Add virtual machines</button>}
      </div>
    </div>

    {powerScan.error && <ErrorNotice error={powerScan.error} />}
    {scan && <p className="text-xs text-slate-600">
      Power state read from Azure at {format(scan.checked_at)} — {scan.scanned} reported{scan.failed > 0 ? `, ${scan.failed} could not be read` : ''}.
      {powerFilter && ` Showing ${rows.length} of ${pageRows.length} on this page.`}
    </p>}

    <div className="flex flex-wrap items-center gap-3">
      <SearchInput value={query} onChange={setQuery} placeholder="Search virtual machines" label="Search virtual machines" />
      <select className="!w-auto" value={enabledFilter} onChange={(event) => setEnabledFilter(event.target.value)} aria-label="Filter by state">
        <option value="">Any state</option><option value="true">Enabled</option><option value="false">Disabled</option>
      </select>
      {!groupId && <>
        <select className="!w-auto" value={connectionFilter} onChange={(event) => setConnectionFilter(event.target.value)} aria-label="Filter by Azure tenant">
          <option value="">Any tenant</option>
          {connections.data?.map((item) => <option key={item.id} value={item.id}>{connectionOptionLabel(item)}</option>)}
        </select>
        {canReadGroups && <select className="!w-auto" value={groupFilter} onChange={(event) => setGroupFilter(event.target.value)} aria-label="Filter by group">
          <option value="">Any group</option>
          {(tree.data ?? []).flatMap(function walk(node): { id: string; label: string }[] { return [{ id: node.id, label: node.name_path }, ...(node.children ?? []).flatMap(walk)] }).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select>}
      </>}
      <select
        className="!w-auto"
        value={powerFilter}
        onChange={(event) => setPowerFilter(event.target.value)}
        aria-label="Filter by power state"
        disabled={!scan}
        title={scan ? 'Power state is live, so this narrows the rows scanned on this page' : 'Scan power state first to filter by it'}
      >
        <option value="">Any power state</option>
        {powerOptions.map((key) => <option key={key} value={key}>{powerLabel(key)}</option>)}
      </select>
      {groupId && canHaveRings && <label className="flex items-center gap-2 text-sm text-slate-700"><input type="checkbox" className="!w-auto" checked={includeRings} onChange={(event) => setIncludeRings(event.target.checked)} />Include rings beneath</label>}
    </div>

    {selected.size > 0 && canWrite && <div className="flex flex-wrap items-center gap-3 rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900" role="region" aria-label="Bulk actions">
      <span className="font-semibold">{selected.size} selected</span>
      {canRunWaves && <>
        <button type="button" className="btn-secondary !py-1" onClick={() => setPowerAction('start')}><Play size={13} />Start now</button>
        <button type="button" className="btn-secondary !py-1" onClick={() => setPowerAction('stop')}><Square size={13} />Stop now</button>
        <span className="h-4 w-px bg-blue-200" aria-hidden="true" />
      </>}
      {canReadGroups && <button type="button" className="btn-secondary !py-1" onClick={() => setMoving(true)}>Move to group</button>}
      <button type="button" className="btn-secondary !py-1" onClick={() => setConfirm('enable')}>Enable</button>
      <button type="button" className="btn-secondary !py-1" onClick={() => setConfirm('disable')}>Disable</button>
      {canRunWaves && <button type="button" className="btn-danger !py-1" onClick={() => setConfirm('delete')}>Delete</button>}
      <button type="button" className="ml-auto link" onClick={() => setSelected(new Set())}>Clear selection</button>
    </div>}

    {bulk.error && <ErrorNotice error={bulk.error} />}
    {list.error && <ErrorNotice error={list.error} />}

    {list.isLoading ? <TableSkeleton columns={6} /> : rows.length === 0 ? <EmptyState
      icon={<Server size={22} />}
      title={powerFilter ? `No virtual machines are ${powerLabel(powerFilter).toLowerCase()}` : 'No virtual machines here yet'}
      description={powerFilter
        ? 'Power state is read live for the page you are looking at. Clear the power filter, or scan another page.'
        : groupId ? 'Add resource IDs directly or discover them from a connected Azure tenant.' : 'Import an inventory CSV or add virtual machines from an application.'}
      action={powerFilter
        ? <button type="button" className="btn-secondary" onClick={() => setPowerFilter('')}>Clear power filter</button>
        : canWrite && groupId ? <button type="button" className="btn-primary" onClick={() => setAdding(true)}><Plus size={16} />Add virtual machines</button> : <Link className="btn-secondary" to="/applications">Open applications</Link>}
    /> : <>
      <div className="surface hidden max-h-[70vh] overflow-auto md:block">
        <table className="u-table">
          <thead><tr>
            {canWrite && <th className="w-10"><input type="checkbox" className="!w-auto" aria-label="Select all rows" checked={allSelected} onChange={(event) => setSelected(event.target.checked ? new Set(rows.map((item) => item.id)) : new Set())} /></th>}
            <SortHeader label="Display name" sortKey="display_name" sort={sort} onSort={toggle} />
            <SortHeader label="VM name" sortKey="vm_name" sort={sort} onSort={toggle} />
            <SortHeader label="Resource group" sortKey="resource_group" sort={sort} onSort={toggle} />
            <SortHeader label="Subscription" sortKey="subscription_id" sort={sort} onSort={toggle} />
            <SortHeader label="Effective connection" />
            <SortHeader label="Power" />
            <SortHeader label="State" sortKey="enabled" sort={sort} onSort={toggle} />
            <SortHeader label="Edit" className="text-right" />
          </tr></thead>
          <tbody>{rows.map((vm) => <tr key={vm.id}>
            {canWrite && <td><input type="checkbox" className="!w-auto" aria-label={`Select ${vm.display_name || vm.vm_name}`} checked={selected.has(vm.id)} onChange={(event) => setSelected((current) => { const next = new Set(current); if (event.target.checked) next.add(vm.id); else next.delete(vm.id); return next })} /></td>}
            {cell(vm)}
          </tr>)}</tbody>
        </table>
      </div>
      <ul className="space-y-3 md:hidden">{rows.map((vm) => <li key={vm.id} className="card space-y-2">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0"><p className="truncate font-medium text-slate-900">{vm.display_name || vm.vm_name}</p><p className="truncate text-xs text-slate-500">{vm.group_path}</p></div>
          {canWrite && <input type="checkbox" className="!w-auto" aria-label={`Select ${vm.display_name || vm.vm_name}`} checked={selected.has(vm.id)} onChange={(event) => setSelected((current) => { const next = new Set(current); if (event.target.checked) next.add(vm.id); else next.delete(vm.id); return next })} />}
        </div>
        <p className="text-xs text-slate-600">{vm.resource_group} · {connectionLabel(vm.effective_connection_name, vm.effective_connection_tenant_id ?? vm.connection_tenant_id)}</p>
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Chip tone={vm.enabled ? 'success' : 'neutral'}>{vm.enabled ? 'Enabled' : 'Disabled'}</Chip>
            <PowerCell result={power[vm.id]} />
            <PortalLink vm={vm} compact />
          </div>
          {canWrite && <button type="button" className="btn-secondary !py-1" onClick={() => setEditing(vm)}><Pencil size={14} />Edit</button>}
        </div>
      </li>)}</ul>
      <div className="surface"><Pagination total={list.data?.total ?? 0} limit={LIMIT} offset={offset} onChange={setOffset} /></div>
    </>}

    <VmEditDrawer vm={editing} onClose={() => setEditing(null)} canReadGroups={canReadGroups} canDelete={canRunWaves} />
    <PowerActionDialog open={powerAction !== null} action={powerAction ?? 'start'} vms={selectedVms} onClose={() => setPowerAction(null)} />
    {groupId && <AddVmsDrawer open={adding} onClose={() => setAdding(false)} groupId={groupId} groupName={groupName ?? 'this group'} />}
    <BulkMoveDrawer open={moving} count={selected.size} busy={bulk.isPending} canReadGroups={canReadGroups} onClose={() => setMoving(false)} onMove={(destination) => bulk.mutate({ vm_ids: [...selected], action: 'move', group_id: destination })} />
    <ConfirmDialog
      open={confirm !== null}
      title={confirm === 'delete' ? 'Delete virtual machines' : confirm === 'enable' ? 'Enable virtual machines' : 'Disable virtual machines'}
      tone={confirm === 'delete' ? 'danger' : 'primary'}
      confirmLabel={confirm === 'delete' ? 'Delete' : confirm === 'enable' ? 'Enable' : 'Disable'}
      busy={bulk.isPending}
      onCancel={() => setConfirm(null)}
      onConfirm={() => confirm && bulk.mutate({ vm_ids: [...selected], action: confirm })}
    >
      <p>This affects <strong>{selected.size}</strong> virtual machine{selected.size === 1 ? '' : 's'} across tenant{tenants.size === 1 ? '' : 's'}: <strong>{[...tenants].join(', ') || 'default'}</strong>.</p>
      {confirm === 'delete' && <p className="mt-2">Deleting also removes schedules that target those VMs directly. This cannot be undone.</p>}
    </ConfirmDialog>
  </section>
}
