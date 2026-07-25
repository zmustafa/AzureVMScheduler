import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, CircleSlash, CloudDownload, Search, Wand2 } from 'lucide-react'
import { api, json } from '../api'
import { Callout } from './Help'
import { Drawer } from './Overlay'
import { ErrorNotice, Field, Chip } from './Ui'
import { connectionOptionLabel, isVmResourceId, splitResourceIds, splitVmNames, useConnections } from '../lib/queries'
import type { DiscoveryResult, ResolvedVmName, VirtualMachine, VmNameResolution } from '../types'

type AddResult = { created: VirtualMachine[]; errors: { vm_resource_id: string; error: string }[] }
type Tab = 'paste' | 'names' | 'discover'

const TAB_LABELS: Record<Tab, string> = { paste: 'Paste resource IDs', names: 'Resolve VM names', discover: 'Browse a subscription' }

function StatusChip({ status }: { status: ResolvedVmName['status'] }) {
  if (status === 'resolved') return <Chip tone="success" icon={<CheckCircle2 size={13} />}>Resolved</Chip>
  if (status === 'ambiguous') return <Chip tone="warn">Multiple matches</Chip>
  return <Chip tone="danger" icon={<CircleSlash size={13} />}>Not found</Chip>
}

/** Add virtual machines to a group by pasting resource IDs, resolving bare names in Azure, or browsing a subscription. */
export function AddVmsDrawer({ open, onClose, groupId, groupName }: { open: boolean; onClose: () => void; groupId: string; groupName: string }) {
  const client = useQueryClient()
  const connections = useConnections()
  const [tab, setTab] = useState<Tab>('paste')
  const [text, setText] = useState('')
  const [nameText, setNameText] = useState('')
  const [connectionId, setConnectionId] = useState('')
  const [subscription, setSubscription] = useState('')
  const [scopeSubscriptions, setScopeSubscriptions] = useState('')
  const [filter, setFilter] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [chosen, setChosen] = useState<Record<string, string>>({})
  const [result, setResult] = useState<AddResult | null>(null)

  useEffect(() => {
    if (open) return
    setText(''); setNameText(''); setFilter(''); setSelected(new Set()); setChosen({}); setResult(null)
  }, [open])

  useEffect(() => {
    const connection = connections.data?.find((item) => item.id === connectionId)
    if (connection?.default_subscription) setSubscription(connection.default_subscription)
  }, [connectionId, connections.data])

  const pasted = useMemo(() => splitResourceIds(text), [text])
  const invalid = useMemo(() => pasted.filter((item) => !isVmResourceId(item)), [pasted])
  const names = useMemo(() => splitVmNames(nameText), [nameText])

  const discover = useMutation({
    mutationFn: () => api<DiscoveryResult>(`/connections/${connectionId}/vms?subscription_id=${encodeURIComponent(subscription.trim())}`),
    onSuccess: () => setSelected(new Set()),
  })

  const resolve = useMutation({
    mutationFn: () => api<VmNameResolution>(`/connections/${connectionId}/resolve-vms`, json('POST', {
      names,
      subscription_ids: splitResourceIds(scopeSubscriptions),
    })),
    onSuccess: (data) => {
      // Pre-select every unambiguous, not-yet-imported match so the common case is a single click.
      const picks: Record<string, string> = {}
      for (const item of data.items) {
        const first = item.matches[0]
        if (item.status === 'resolved' && first && !first.already_imported) picks[item.query] = first.vm_resource_id
      }
      setChosen(picks)
    },
  })

  const add = useMutation({
    mutationFn: (ids: string[]) => api<AddResult>(`/groups/${groupId}/vms`, json('POST', { vm_resource_ids: ids, azure_connection_id: connectionId || null, enabled: true, notes: '' })),
    onSuccess: (data) => {
      setResult(data)
      void client.invalidateQueries({ queryKey: ['vms'] })
      void client.invalidateQueries({ queryKey: ['groups'] })
      void client.invalidateQueries({ queryKey: ['group'] })
      if (!data.errors.length) { setText(''); setNameText(''); setSelected(new Set()); setChosen({}); resolve.reset() }
    },
  })

  const discovered = discover.data?.items ?? []
  const visible = discovered.filter((item) => `${item.name} ${item.resource_group} ${item.location}`.toLowerCase().includes(filter.trim().toLowerCase()))
  const chosenIds = useMemo(() => [...new Set(Object.values(chosen).filter(Boolean))], [chosen])
  const importable = pasted.length > 0 && invalid.length === 0
  const canSubmit = tab === 'paste' ? importable : tab === 'names' ? chosenIds.length > 0 : selected.size > 0
  const submit = () => add.mutate(tab === 'paste' ? pasted : tab === 'names' ? chosenIds : [...selected])
  const submitLabel = add.isPending ? 'Adding…'
    : tab === 'paste' ? `Add ${pasted.length || ''} VM${pasted.length === 1 ? '' : 's'}`.trim()
      : tab === 'names' ? `Add ${chosenIds.length} matched VM${chosenIds.length === 1 ? '' : 's'}`
        : `Import ${selected.size} selected`

  return <Drawer
    open={open}
    onClose={onClose}
    width="max-w-3xl"
    title="Add virtual machines"
    description={`They are added to ${groupName}.`}
    footer={<>
      <button type="button" className="btn-secondary" onClick={onClose}>Close</button>
      <button type="button" className="btn-primary" disabled={!canSubmit || add.isPending} onClick={submit}>{submitLabel}</button>
    </>}
  >
    <div className="space-y-4">
      <div className="inline-flex flex-wrap rounded-lg border border-slate-300 bg-white p-0.5" role="tablist" aria-label="Add method">
        {(Object.keys(TAB_LABELS) as Tab[]).map((key) => <button key={key} role="tab" type="button" aria-selected={tab === key} onClick={() => setTab(key)} className={`rounded-md px-3 py-1.5 text-sm font-semibold transition ${tab === key ? 'bg-blue-100 text-blue-800' : 'text-slate-600 hover:bg-slate-50'}`}>{TAB_LABELS[key]}</button>)}
      </div>

      {add.error && <ErrorNotice error={add.error} />}
      {result && <Callout tone={result.errors.length ? 'warn' : 'success'} title={`${result.created.length} virtual machine(s) added`}>
        {result.errors.length ? <ul className="mt-1 space-y-1">{result.errors.map((item) => <li key={item.vm_resource_id}><code className="font-mono">{item.vm_resource_id.split('/').pop()}</code> — {item.error}</li>)}</ul> : 'Every row was accepted.'}
      </Callout>}

      <Field label="Azure tenant for these VMs" hint={tab === 'paste' ? 'Optional override. Leave blank to inherit the group or default connection.' : 'Required — the lookup runs against this tenant, and new VMs inherit it as their override.'}>
        <select value={connectionId} onChange={(event) => setConnectionId(event.target.value)}>
          <option value="">Inherit from group / default</option>
          {connections.data?.map((item) => <option key={item.id} value={item.id}>{connectionOptionLabel(item)}</option>)}
        </select>
      </Field>

      {tab === 'paste' && <div className="space-y-3">
        <Field label="Resource IDs" hint="One per line. Format: /subscriptions/…/resourceGroups/…/providers/Microsoft.Compute/virtualMachines/…">
          <textarea rows={8} className="font-mono text-xs" value={text} onChange={(event) => setText(event.target.value)} placeholder="/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm-web-01" />
        </Field>
        <div className="flex flex-wrap items-center gap-2">
          <Chip tone={pasted.length ? 'info' : 'neutral'}>{pasted.length} line{pasted.length === 1 ? '' : 's'}</Chip>
          {invalid.length > 0
            ? <Chip tone="danger" icon={<CircleSlash size={13} />}>{invalid.length} malformed</Chip>
            : pasted.length > 0 && <Chip tone="success" icon={<CheckCircle2 size={13} />}>All resource IDs look valid</Chip>}
        </div>
        {invalid.length > 0 && <Callout tone="warn" title="Fix these lines before adding">
          <ul className="space-y-1">{invalid.slice(0, 8).map((item) => <li key={item} className="break-all font-mono">{item}</li>)}</ul>
        </Callout>}
      </div>}

      {tab === 'names' && <div className="space-y-3">
        <Callout tone="info" title="Paste plain VM names">
          Azure VM Scheduler searches the selected tenant with Azure Resource Graph and works out each machine&apos;s subscription and resource group for you. Review the matches below, then add them.
        </Callout>
        <Field label="Virtual machine names" hint="One per line — commas, semicolons, tabs and spaces also work. Duplicates are removed.">
          <textarea rows={7} className="font-mono text-xs" value={nameText} onChange={(event) => setNameText(event.target.value)} placeholder={'vm-web-01\nvm-web-02\nvm-api-01'} />
        </Field>
        <Field label="Limit to subscriptions" hint="Optional. Comma or newline separated subscription IDs. Leave blank to search every subscription the tenant can see.">
          <input className="font-mono text-xs" autoComplete="off" value={scopeSubscriptions} onChange={(event) => setScopeSubscriptions(event.target.value)} placeholder="00000000-0000-0000-0000-000000000000" />
        </Field>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn-primary" disabled={!connectionId || names.length === 0 || resolve.isPending} onClick={() => resolve.mutate()}>
            <Wand2 size={16} />{resolve.isPending ? 'Searching Azure…' : `Resolve ${names.length || ''} name${names.length === 1 ? '' : 's'}`.replace('  ', ' ')}
          </button>
          <Chip tone={names.length ? 'info' : 'neutral'}>{names.length} unique name{names.length === 1 ? '' : 's'}</Chip>
        </div>
        {!connectionId && <Callout tone="warn" title="Choose a tenant first">Name resolution calls Azure Resource Manager with the credentials stored on that connection.</Callout>}
        {resolve.error && <Callout tone="warn" title="Azure name resolution failed">
          <p>{resolve.error instanceof Error ? resolve.error.message : 'The lookup could not be completed.'}</p>
          <p className="mt-1.5">This needs a working tenant connection with reader access. The identity also needs <code className="font-mono">Microsoft.ResourceGraph/resources/read</code>; without it Azure VM Scheduler falls back to scanning each subscription, which requires reader access on them.</p>
        </Callout>}

        {resolve.data && <>
          <div className="flex flex-wrap items-center gap-2">
            <Chip tone="success">{resolve.data.resolved} resolved</Chip>
            {resolve.data.ambiguous > 0 && <Chip tone="warn">{resolve.data.ambiguous} ambiguous</Chip>}
            {resolve.data.not_found > 0 && <Chip tone="danger">{resolve.data.not_found} not found</Chip>}
            <Chip tone="neutral">{resolve.data.source === 'resource_graph' ? 'via Resource Graph' : 'via subscription scan'}</Chip>
          </div>
          <ul className="divide-y divide-slate-200 overflow-hidden rounded-lg border border-slate-200">
            {resolve.data.items.map((item) => <li key={item.query} className="p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="font-mono text-sm font-semibold text-slate-900">{item.query}</p>
                <StatusChip status={item.status} />
              </div>
              {item.status === 'not_found' && <p className="mt-1.5 text-xs text-slate-600">No virtual machine with this name is visible to the selected tenant. Check the spelling, the tenant, or the subscription scope.</p>}
              {item.matches.length > 0 && <div className="mt-2 space-y-1.5">
                {item.status === 'ambiguous' && <p className="text-xs text-amber-800">This name exists in more than one place. Pick the correct one.</p>}
                {item.matches.map((match) => {
                  const picked = chosen[item.query] === match.vm_resource_id
                  return <label key={match.vm_resource_id} className={`flex items-start gap-3 rounded-lg border p-2.5 transition ${match.already_imported ? 'border-slate-200 bg-slate-50' : picked ? 'cursor-pointer border-blue-300 bg-blue-50/60' : 'cursor-pointer border-slate-200 hover:bg-slate-50'}`}>
                    <input
                      type="radio"
                      className="!w-auto mt-0.5"
                      name={`match-${item.query}`}
                      disabled={match.already_imported}
                      checked={picked}
                      onChange={() => setChosen((current) => ({ ...current, [item.query]: match.vm_resource_id }))}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium text-slate-900">{match.resource_group}<span className="text-slate-400"> / </span>{match.name}</span>
                      <span className="block truncate text-xs text-slate-500">{match.subscription_name ? `${match.subscription_name} · ` : ''}{match.subscription_id}{match.location ? ` · ${match.location}` : ''}</span>
                      {match.already_imported && <span className="mt-1 block text-xs text-slate-600">Already in inventory{match.group_path ? ` under ${match.group_path}` : ''}.</span>}
                    </span>
                    {!match.already_imported && picked && <Chip tone="info">Will be added</Chip>}
                  </label>
                })}
                {chosen[item.query] && <button type="button" className="text-xs font-semibold text-slate-500 hover:text-slate-800" onClick={() => setChosen((current) => { const next = { ...current }; delete next[item.query]; return next })}>Skip this name</button>}
              </div>}
            </li>)}
          </ul>
        </>}
      </div>}

      {tab === 'discover' && <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
          <Field label="Subscription ID"><input value={subscription} onChange={(event) => setSubscription(event.target.value)} placeholder="00000000-0000-0000-0000-000000000000" /></Field>
          <button type="button" className="btn-primary" disabled={!connectionId || !subscription.trim() || discover.isPending} onClick={() => discover.mutate()}><CloudDownload size={16} />{discover.isPending ? 'Querying Azure…' : 'Discover VMs'}</button>
        </div>
        {!connectionId && <Callout tone="info" title="Choose a tenant first">Live discovery calls Azure Resource Manager with the credentials stored on that connection.</Callout>}
        {discover.error && <Callout tone="warn" title="Azure discovery failed">
          <p>{discover.error instanceof Error ? discover.error.message : 'The lookup could not be completed.'}</p>
          <p className="mt-1.5">Discovery needs a working tenant connection with real credentials and reader access to the subscription. Demo or placeholder connections cannot reach Azure — use the “Paste resource IDs” tab instead.</p>
        </Callout>}
        {discover.data && <>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-2.5 text-slate-500" size={16} aria-hidden="true" />
            <input className="!pl-9" autoComplete="off" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Filter discovered VMs" aria-label="Filter discovered virtual machines" />
          </div>
          <p className="muted">{discover.data.count} virtual machine(s) found in {discover.data.subscription_id}.</p>
          <ul className="max-h-80 divide-y divide-slate-200 overflow-y-auto rounded-lg border border-slate-200">
            {visible.map((item) => <li key={item.id}>
              <label className={`flex items-center gap-3 px-3 py-2 ${item.already_imported ? 'bg-slate-50' : 'hover:bg-blue-50/50'}`}>
                <input type="checkbox" className="!w-auto" disabled={item.already_imported} checked={selected.has(item.id)} onChange={(event) => setSelected((current) => { const next = new Set(current); if (event.target.checked) next.add(item.id); else next.delete(item.id); return next })} />
                <span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium text-slate-900">{item.name}</span><span className="block truncate text-xs text-slate-500">{item.resource_group} · {item.location}{item.power_state ? ` · ${item.power_state}` : ''}</span></span>
                {item.already_imported && <Chip tone="neutral">Already imported</Chip>}
              </label>
            </li>)}
            {visible.length === 0 && <li className="px-3 py-4 text-sm text-slate-500">No discovered virtual machines match this filter.</li>}
          </ul>
        </>}
      </div>}
    </div>
  </Drawer>
}
