import { useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router'
import { ArrowLeft, CheckCircle2, CircleSlash, FolderPlus, Layers, ListChecks, MapPin, Wand2 } from 'lucide-react'
import { api, json } from '../api'
import { useCan } from '../auth'
import { connectionLabel, connectionOptionLabel, isVmResourceId, splitVmNames, useConnections, useGroupTree } from '../lib/queries'
import { Callout } from '../components/Help'
import { GroupPicker } from '../components/GroupPicker'
import { Chip, ErrorNotice, Field, PageHeader } from '../components/Ui'
import type { Group, VirtualMachine, VmNameResolution } from '../types'

type LookupMatch = VirtualMachine & { group_path: string }
type LookupItem = { query: string; status: 'known' | 'unknown'; matches: LookupMatch[] }
type LookupResult = { requested: number; known: number; unknown: number; items: LookupItem[] }
type AddResult = { created: VirtualMachine[]; errors: { vm_resource_id: string; error: string }[] }
type Destination = 'existing' | 'new'

/**
 * Bulk placement: paste any list of VM names, see which ones already live in an application,
 * and file the rest into an existing group or a brand new application in one pass.
 */
export function LocateVmsPage() {
  const canWriteGroups = useCan('groups.write')
  const canWriteVms = useCan('vms.write')
  const client = useQueryClient()
  const navigate = useNavigate()
  const tree = useGroupTree()
  const connections = useConnections()

  const [text, setText] = useState('')
  const [destination, setDestination] = useState<Destination>('existing')
  const [groupId, setGroupId] = useState<string | null>(null)
  const [appName, setAppName] = useState('')
  const [ringName, setRingName] = useState('')
  const [connectionId, setConnectionId] = useState('')
  const [chosen, setChosen] = useState<Record<string, string>>({})
  const [added, setAdded] = useState<{ count: number; groupName: string; groupId: string } | null>(null)

  const names = useMemo(() => splitVmNames(text), [text])

  const lookup = useMutation({
    mutationFn: () => api<LookupResult>('/vms/lookup', json('POST', { names })),
    onSuccess: (data) => {
      // Anything pasted as a full resource ID is ready to add without touching Azure.
      const picks: Record<string, string> = {}
      for (const item of data.items) {
        if (item.status === 'unknown' && isVmResourceId(item.query)) picks[item.query] = item.query.trim()
      }
      setChosen(picks)
      resolve.reset()
      setAdded(null)
    },
  })

  const unknownNames = useMemo(() => (lookup.data?.items ?? []).filter((item) => item.status === 'unknown').map((item) => item.query), [lookup.data])
  const knownItems = useMemo(() => (lookup.data?.items ?? []).filter((item) => item.status === 'known'), [lookup.data])
  const needsAzure = useMemo(() => unknownNames.filter((name) => !isVmResourceId(name)), [unknownNames])

  const resolve = useMutation({
    mutationFn: () => api<VmNameResolution>(`/connections/${connectionId}/resolve-vms`, json('POST', { names: needsAzure, subscription_ids: [] })),
    onSuccess: (data) => {
      setChosen((current) => {
        const next = { ...current }
        for (const item of data.items) {
          const first = item.matches[0]
          if (item.status === 'resolved' && first && !first.already_imported) next[item.query] = first.vm_resource_id
        }
        return next
      })
    },
  })

  const place = useMutation({
    mutationFn: async (ids: string[]) => {
      let targetId = groupId
      let targetName = tree.data ? findName(tree.data, groupId) : ''
      if (destination === 'new') {
        const application = await api<Group>('/groups', json('POST', { name: appName.trim(), description: '', azure_connection_id: connectionId || null }))
        targetId = application.id
        targetName = application.name
        if (ringName.trim()) {
          const ring = await api<Group>('/groups', json('POST', { name: ringName.trim(), parent_id: application.id }))
          targetId = ring.id
          targetName = `${application.name} / ${ring.name}`
        }
      }
      if (!targetId) throw new Error('Choose where these virtual machines should go')
      const result = await api<AddResult>(`/groups/${targetId}/vms`, json('POST', { vm_resource_ids: ids, azure_connection_id: connectionId || null, enabled: true, notes: '' }))
      return { result, targetId, targetName }
    },
    onSuccess: ({ result, targetId, targetName }) => {
      setAdded({ count: result.created.length, groupName: targetName, groupId: targetId })
      setChosen({})
      void client.invalidateQueries({ queryKey: ['groups'] })
      void client.invalidateQueries({ queryKey: ['vms'] })
      void lookup.mutateAsync().catch(() => undefined)
    },
  })

  const readyIds = useMemo(() => [...new Set(Object.values(chosen).filter(Boolean))], [chosen])
  const destinationReady = destination === 'existing' ? Boolean(groupId) : appName.trim().length > 0
  const canPlace = canWriteVms && readyIds.length > 0 && destinationReady

  return <>
    <Link to="/applications" className="mb-4 inline-flex items-center gap-2 text-sm text-slate-600 hover:text-blue-700"><ArrowLeft size={16} />Applications</Link>
    <PageHeader
      title="Locate and place virtual machines"
      description="Paste any list of VM names to see which are already covered by an application and file the rest in one pass."
    />

    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.35fr)]">
      <section className="card space-y-4">
        <div className="flex items-center gap-3"><MapPin className="text-blue-700" aria-hidden="true" /><div><h2 className="font-semibold">1. Paste the list</h2><p className="muted">Names or full resource IDs</p></div></div>
        <Field label="Virtual machines" hint="One per line — commas, semicolons, tabs and spaces also work. Duplicates and case differences are collapsed.">
          <textarea rows={12} className="font-mono text-xs" value={text} onChange={(event) => setText(event.target.value)} placeholder={'vm-web-01\nvm-web-02\nvm-api-01'} />
        </Field>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn-primary" disabled={names.length === 0 || lookup.isPending} onClick={() => lookup.mutate()}>
            <ListChecks size={16} />{lookup.isPending ? 'Checking…' : `Check ${names.length || ''} name${names.length === 1 ? '' : 's'}`.replace('  ', ' ')}
          </button>
          <Chip tone={names.length ? 'info' : 'neutral'}>{names.length} unique</Chip>
        </div>
        {lookup.error && <ErrorNotice error={lookup.error} />}
      </section>

      <section className="space-y-6">
        {!lookup.data && <Callout tone="info" title="Nothing checked yet">
          The check runs against the local inventory only — no Azure call is made. Azure is contacted later, and only if unmatched names need their subscription and resource group resolved.
        </Callout>}

        {lookup.data && <>
          <div className="flex flex-wrap items-center gap-2">
            <Chip tone="success" icon={<CheckCircle2 size={13} />}>{lookup.data.known} already placed</Chip>
            <Chip tone={lookup.data.unknown ? 'warn' : 'neutral'} icon={<CircleSlash size={13} />}>{lookup.data.unknown} not in any application</Chip>
            <Chip tone="neutral">{lookup.data.requested} checked</Chip>
          </div>

          {added && <Callout tone="success" title={`${added.count} virtual machine(s) added to ${added.groupName}`}>
            <button type="button" className="font-semibold underline" onClick={() => navigate(`/applications/${added.groupId}`)}>Open {added.groupName}</button>
          </Callout>}

          <section className="card">
            <h2 className="font-semibold">Already in an application</h2>
            <p className="muted">These are covered today — open the group to see or change its schedule.</p>
            {knownItems.length === 0 ? <p className="mt-4 text-sm text-slate-500">None of the pasted machines are in the inventory yet.</p> : <ul className="mt-4 divide-y divide-slate-200">
              {knownItems.map((item) => <li key={item.query} className="py-2.5">
                {item.matches.map((match) => <div key={match.id} className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-slate-900">{match.display_name || match.vm_name}</p>
                    <p className="truncate text-xs text-slate-500">{match.resource_group} · {connectionLabel(match.effective_connection_name ?? match.connection_name, match.connection_tenant_id)}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    {!match.enabled && <Chip tone="neutral">Disabled</Chip>}
                    <Link to={`/applications/${match.group_id}`} className="inline-flex items-center gap-1 rounded-full border border-blue-200 bg-blue-50 px-2.5 py-1 text-xs font-semibold text-blue-800 hover:bg-blue-100">
                      <Layers size={12} aria-hidden="true" />{match.group_path}
                    </Link>
                  </div>
                </div>)}
              </li>)}
            </ul>}
          </section>

          <section className="card space-y-4">
            <div><h2 className="font-semibold">Not in any application</h2><p className="muted">Choose a destination, resolve them in Azure if needed, then add them.</p></div>

            {unknownNames.length === 0 ? <p className="text-sm text-slate-500">Every pasted machine is already in the inventory. Nothing to place.</p> : <>
              <ul className="flex flex-wrap gap-1.5">
                {unknownNames.map((name) => <li key={name}><Chip tone={chosen[name] ? 'success' : 'warn'}>{name}{chosen[name] ? ' · ready' : ''}</Chip></li>)}
              </ul>

              {needsAzure.length > 0 && <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
                <p className="text-sm font-semibold text-slate-800">Resolve {needsAzure.length} name{needsAzure.length === 1 ? '' : 's'} in Azure</p>
                <p className="text-xs text-slate-600">Bare names need their subscription and resource group looked up before they can be added. Pasting full resource IDs skips this step.</p>
                <Field label="Azure tenant">
                  <select value={connectionId} onChange={(event) => setConnectionId(event.target.value)}>
                    <option value="">Select a tenant</option>
                    {connections.data?.map((item) => <option key={item.id} value={item.id}>{connectionOptionLabel(item)}</option>)}
                  </select>
                </Field>
                <button type="button" className="btn-secondary" disabled={!connectionId || resolve.isPending} onClick={() => resolve.mutate()}>
                  <Wand2 size={15} />{resolve.isPending ? 'Searching Azure…' : 'Resolve in Azure'}
                </button>
                {resolve.error && <Callout tone="warn" title="Azure name resolution failed">
                  <p>{resolve.error instanceof Error ? resolve.error.message : 'The lookup could not be completed.'}</p>
                  <p className="mt-1.5">A working tenant connection with reader access is required. You can also paste full resource IDs instead.</p>
                </Callout>}
                {resolve.data && <div className="space-y-2">
                  <div className="flex flex-wrap gap-2">
                    <Chip tone="success">{resolve.data.resolved} resolved</Chip>
                    {resolve.data.ambiguous > 0 && <Chip tone="warn">{resolve.data.ambiguous} ambiguous</Chip>}
                    {resolve.data.not_found > 0 && <Chip tone="danger">{resolve.data.not_found} not found</Chip>}
                  </div>
                  {resolve.data.items.filter((item) => item.matches.length > 1 || item.status === 'not_found').map((item) => <div key={item.query} className="rounded-lg border border-slate-200 bg-white p-2.5">
                    <p className="font-mono text-xs font-semibold text-slate-900">{item.query}</p>
                    {item.status === 'not_found'
                      ? <p className="mt-1 text-xs text-slate-600">Not visible to this tenant. Check the spelling or the tenant.</p>
                      : <div className="mt-1.5 space-y-1">
                        <p className="text-xs text-amber-800">Several machines share this name — pick one.</p>
                        {item.matches.map((match) => <label key={match.vm_resource_id} className="flex cursor-pointer items-center gap-2 rounded border border-slate-200 px-2 py-1.5 text-xs hover:bg-slate-50">
                          <input type="radio" className="!w-auto" name={`pick-${item.query}`} checked={chosen[item.query] === match.vm_resource_id} onChange={() => setChosen((current) => ({ ...current, [item.query]: match.vm_resource_id }))} />
                          <span className="min-w-0 flex-1 truncate">{match.resource_group} · {match.subscription_name ?? match.subscription_id}</span>
                        </label>)}
                      </div>}
                  </div>)}
                </div>}
              </div>}

              <fieldset className="space-y-3">
                <legend className="text-sm font-semibold text-slate-800">Destination</legend>
                <div className="flex flex-wrap gap-2">
                  {([['existing', 'Existing application or ring'], ['new', 'Create a new application']] as const).map(([key, label]) => <label key={key} className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-sm transition ${destination === key ? 'border-blue-300 bg-blue-50/60 font-semibold text-blue-900' : 'border-slate-200 hover:bg-slate-50'}`}>
                    <input type="radio" className="!w-auto" name="destination" checked={destination === key} onChange={() => setDestination(key)} />
                    {label}
                  </label>)}
                </div>

                {destination === 'existing'
                  ? <GroupPicker nodes={tree.data ?? []} value={groupId} onChange={setGroupId} label="Destination group" />
                  : <div className="grid gap-3 sm:grid-cols-2">
                    <Field label="New application name"><input value={appName} onChange={(event) => setAppName(event.target.value)} placeholder="ABC app" /></Field>
                    <Field label="First ring (optional)" hint="Leave blank to put the VMs directly on the application."><input value={ringName} onChange={(event) => setRingName(event.target.value)} placeholder="ring1" /></Field>
                  </div>}
                {destination === 'new' && !canWriteGroups && <Callout tone="warn" title="Permission required">Creating an application needs the groups.write permission.</Callout>}
              </fieldset>

              {place.error && <ErrorNotice error={place.error} />}
              <div className="flex flex-wrap items-center gap-3">
                <button type="button" className="btn-primary" disabled={!canPlace || place.isPending} onClick={() => place.mutate(readyIds)}>
                  <FolderPlus size={16} />{place.isPending ? 'Adding…' : `Add ${readyIds.length} VM${readyIds.length === 1 ? '' : 's'}`}
                </button>
                {readyIds.length < unknownNames.length && <p className="text-xs text-slate-600">{unknownNames.length - readyIds.length} still need a resource ID.</p>}
              </div>
            </>}
          </section>
        </>}
      </section>
    </div>
  </>
}

function findName(nodes: { id: string; name: string; children?: unknown[] }[], id: string | null): string {
  if (!id) return ''
  for (const node of nodes) {
    if (node.id === id) return node.name
    const nested = findName((node.children ?? []) as { id: string; name: string; children?: unknown[] }[], id)
    if (nested) return nested
  }
  return ''
}
