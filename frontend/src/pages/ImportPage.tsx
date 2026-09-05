import { useMemo, useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, FileUp, Layers, Server, UploadCloud, Wand2 } from 'lucide-react'
import { api, json } from '../api'
import { useCan } from '../auth'
import { connectionOptionLabel, flattenGroups, useConnections, useGroupTree } from '../lib/queries'
import { Callout, CmdBlock, SetupGuide, Step } from '../components/Help'
import { Chip, EmptyState, ErrorNotice, Field, PageHeader } from '../components/Ui'
import type { ImportCommitResult, ImportPreview, ImportRow } from '../types'

const SIMPLE_CSV = ['vm_name', 'pay-api-01', 'pay-api-02', 'pay-batch-01'].join('\n')

const EXAMPLE_CSV = [
  'application,ring_path,vm_resource_id,display_name,enabled,never_stop,notes,azure_connection',
  'Payments,ring1,/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-pay/providers/Microsoft.Compute/virtualMachines/pay-api-01,Payments API 01,true,false,First wave,Zava Production',
  'Payments,ring2,/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-pay/providers/Microsoft.Compute/virtualMachines/pay-batch-01,,true,true,Never safe to stop,',
  'Ledger,,/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-ledger/providers/Microsoft.Compute/virtualMachines/ledger-01,,false,false,Onboarding pending,',
].join('\n')

const NAMES_CSV = ['application,ring_path,vm_name', 'Payments,ring1,pay-api-01', 'Payments,ring1,pay-api-02', 'Payments,ring2,pay-batch-01'].join('\n')

const COLUMNS = [
  { name: 'vm_name', required: 'Either', desc: 'Bare VM name. Azure VM Scheduler looks it up in the selected tenant and fills in the subscription and resource group for you.' },
  { name: 'vm_resource_id', required: 'Either', desc: 'Full Azure resource id. Use this instead of vm_name when you already have it, or to settle a duplicate name.' },
  { name: 'application', required: 'Optional', desc: 'Top-level application. Created if it does not exist. Omit the column entirely and choose a destination below instead.' },
  { name: 'ring_path', required: 'Optional', desc: 'Ring name inside the application, e.g. ring1. Rings do not nest. Leave blank to attach the VM directly to the application.' },
  { name: 'display_name', required: 'Optional', desc: 'Friendly name shown in the UI. Defaults to the VM name.' },
  { name: 'enabled', required: 'Optional', desc: 'true/false, yes/no or 1/0. Disabled VMs stay in the inventory but are skipped by every schedule.' },
  { name: 'never_stop', required: 'Optional', desc: 'true/false. Stop waves and on-demand stops can never touch the machine. Starts are unaffected.' },
  { name: 'notes', required: 'Optional', desc: 'Free text kept alongside the VM.' },
  { name: 'azure_connection', required: 'Optional', desc: 'Azure tenant connection name or id. Leave blank to inherit the group or the resolving tenant.' },
] as const

function ImportGuide() {
  return <SetupGuide title="Setup guide — inventory CSV format" subtitle="names or resource ids" defaultOpen>
    <Callout tone="success" title="The simplest file that works">
      One column of VM names. Pick the Azure tenant and the destination application or ring below, and Azure VM Scheduler resolves each name to its full resource id, subscription and resource group.
      <CmdBlock cmd={SIMPLE_CSV} />
    </Callout>
    <Callout tone="info">The CSV describes <strong>inventory only</strong>: applications, rings and virtual machines. Schedules are attached to a ring (or application) afterwards in the UI — there is no schedule column, so one ring can be re-timed without re-importing anything.</Callout>

    <div className="mt-3 overflow-x-auto rounded-lg border border-slate-200">
      <table className="w-full text-left text-xs">
        <thead className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-500"><tr><th className="px-3 py-2 font-semibold">Column</th><th className="px-3 py-2 font-semibold">Required</th><th className="px-3 py-2 font-semibold">Meaning</th></tr></thead>
        <tbody className="divide-y divide-slate-200">{COLUMNS.map((column) => <tr key={column.name}>
          <td className="whitespace-nowrap px-3 py-2 font-mono text-slate-800">{column.name}</td>
          <td className="whitespace-nowrap px-3 py-2 text-slate-500">{column.required}</td>
          <td className="px-3 py-2 text-slate-600">{column.desc}</td>
        </tr>)}</tbody>
      </table>
    </div>

    <ol className="mt-4 space-y-4">
      <Step n={1} title="Build the file">Save as UTF-8 CSV, maximum 2 MB. Every row needs <strong>either</strong> <code className="font-mono">vm_name</code> <strong>or</strong> <code className="font-mono">vm_resource_id</code>. Column order does not matter, but unknown column names are rejected outright.<CmdBlock cmd={NAMES_CSV} /><p className="mt-2">The long form, when you already hold the resource ids and want per-row tenants:</p><CmdBlock cmd={EXAMPLE_CSV} /></Step>
      <Step n={2} title="Preview">Bare names are resolved in a single Azure Resource Graph query for the whole file. Rows are then validated for the resource-id format, duplicates inside the file, and VMs already in the inventory. Nothing is written during a preview.</Step>
      <Step n={3} title="Commit atomically">The commit is bound to the preview token, so the exact rows you reviewed are the rows that get written. If any row is invalid the whole import is rejected and nothing is created.</Step>
      <Step n={4} title="Attach schedules in the UI">Open the application, pick a ring, and create a one-time or daily schedule against it. Every VM under the ring inherits that wave, including VMs imported later.</Step>
    </ol>

    <Callout tone="warn" title="Common rejections">
      <ul className="list-disc space-y-1 pl-4">
        <li><strong>Select the Azure tenant to resolve VM names against</strong> — the file uses <code className="font-mono">vm_name</code> but no tenant was chosen.</li>
        <li><strong>&apos;name&apos; exists in N places</strong> — the same VM name appears in several resource groups or subscriptions. Add a <code className="font-mono">vm_resource_id</code> for that row.</li>
        <li><strong>No virtual machine named &apos;…&apos; is visible</strong> — a typo, or the tenant cannot see that VM.</li>
        <li><strong>This VM is already in the inventory</strong> — the resource id already exists; edit it on the VMs page instead.</li>
        <li><strong>Duplicate VM in this CSV</strong> — the same VM appears twice in the file.</li>
        <li><strong>ring_path must be a single ring name</strong> — rings hold virtual machines, not other rings.</li>
      </ul>
    </Callout>
  </SetupGuide>
}

function Summary({ preview }: { preview: ImportPreview }) {
  if (preview.format !== 'inventory') {
    return <p className="text-sm text-slate-700"><strong>{preview.total}</strong> schedule row{preview.total === 1 ? '' : 's'} in the legacy format.</p>
  }
  return <div className="flex flex-wrap items-center gap-2">
    <Chip tone="info" icon={<Layers size={12} />}>{preview.applications_to_create ?? 0} application{(preview.applications_to_create ?? 0) === 1 ? '' : 's'} to create</Chip>
    <Chip tone="info">{preview.rings_to_create ?? 0} ring{(preview.rings_to_create ?? 0) === 1 ? '' : 's'} to create</Chip>
    <Chip tone="success" icon={<Server size={12} />}>{preview.vms_to_create ?? 0} VM{(preview.vms_to_create ?? 0) === 1 ? '' : 's'} to create</Chip>
    {(preview.resolved_from_names ?? 0) > 0 && <Chip tone="accent" icon={<Wand2 size={12} />}>{preview.resolved_from_names} name{preview.resolved_from_names === 1 ? '' : 's'} resolved in Azure</Chip>}
  </div>
}

function RowCard({ row }: { row: ImportRow }) {
  const data = row.data as Record<string, string | boolean | null>
  const ring = String(data.ring_path ?? '')
  const path = [String(data.application ?? ''), ...ring.split('/').filter(Boolean)].filter(Boolean).join(' / ')
  return <li className={`rounded-lg border p-3 ${row.valid ? 'border-slate-200 bg-white' : 'border-rose-200 bg-rose-50'}`}>
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div className="min-w-0">
        <p className="text-sm font-medium text-slate-900">Row {row.row_number} — {String(data.display_name || data.name || 'Unnamed')}</p>
        <p className="truncate text-xs text-slate-500" title={String(data.vm_resource_id ?? '')}>{String(data.vm_resource_id ?? '')}</p>
      </div>
      <Chip tone={row.valid ? 'success' : 'danger'} icon={row.valid ? <CheckCircle2 size={12} /> : undefined}>{row.valid ? 'Ready' : 'Invalid'}</Chip>
    </div>
    {path && <p className="mt-1 text-xs text-slate-600">{path}</p>}
    {row.resolved_from_name && <p className="mt-1 inline-flex items-center gap-1 text-xs text-blue-700"><Wand2 size={12} aria-hidden="true" />Resolved from the VM name in Azure.</p>}
    {data.enabled === false && <p className="mt-1 text-xs text-amber-800">Imported as disabled.</p>}
    {row.errors.map((error) => <p className="mt-1 text-sm text-rose-800" key={error}>{error}</p>)}
  </li>
}

/** CSV inventory import: upload, per-row preview, then an atomic commit bound to the preview token. */
export function ImportPage() {
  const client = useQueryClient()
  const canImport = useCan('imports.write')
  const canReadGroups = useCan('groups.read')
  const connections = useConnections()
  const groupTree = useGroupTree(canReadGroups)
  const [preview, setPreview] = useState<ImportPreview>()
  const [result, setResult] = useState<string>()
  const [invalidOnly, setInvalidOnly] = useState(false)
  const [connectionId, setConnectionId] = useState('')
  const [destinationId, setDestinationId] = useState('')

  const groups = useMemo(() => flattenGroups(groupTree.data ?? []), [groupTree.data])

  const inspect = useMutation({
    mutationFn: (file: File) => {
      const body = new FormData()
      body.set('file', file)
      if (connectionId) body.set('connection_id', connectionId)
      if (destinationId) body.set('default_group_id', destinationId)
      return api<ImportPreview>('/imports/preview', { method: 'POST', body })
    },
    onSuccess: (data) => { setResult(undefined); setInvalidOnly(false); setPreview(data) },
  })

  const commit = useMutation({
    mutationFn: () => api<ImportCommitResult>('/imports/commit', json('POST', { filename: preview?.filename, preview_token: preview?.preview_token, rows: preview?.rows.map((row) => row.data), reject_all: true })),
    onSuccess: (data) => {
      setResult(data.format === 'inventory'
        ? `Imported ${data.accepted} virtual machine${data.accepted === 1 ? '' : 's'} and created ${data.groups_created ?? 0} group${(data.groups_created ?? 0) === 1 ? '' : 's'}.`
        : `Created ${data.accepted} schedule${data.accepted === 1 ? '' : 's'}.`)
      setPreview(undefined)
      void client.invalidateQueries({ queryKey: ['groups'] })
      void client.invalidateQueries({ queryKey: ['vms'] })
      void client.invalidateQueries({ queryKey: ['schedules'] })
      void client.invalidateQueries({ queryKey: ['dashboard'] })
    },
  })

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const file = new FormData(event.currentTarget).get('file')
    if (file instanceof File && file.size) inspect.mutate(file)
  }

  const rows = useMemo(() => (preview?.rows ?? []).filter((row) => (invalidOnly ? !row.valid : true)), [preview, invalidOnly])

  return <>
    <PageHeader title="CSV import" description="Load applications, rings and virtual machines in one atomic pass." />

    <div className="mb-6"><ImportGuide /></div>

    <div className="card mb-6">
      <form className="space-y-4" onSubmit={submit}>
        <div className="flex flex-col gap-4 sm:flex-row sm:items-end">
          <div className="field flex-1">
            <label htmlFor="csv-file">UTF-8 CSV file (max 2 MB)</label>
            <input id="csv-file" name="file" type="file" accept=".csv,text/csv" required disabled={!canImport} />
          </div>
          <button className="btn-primary" disabled={!canImport || inspect.isPending}><UploadCloud size={17} />{inspect.isPending ? 'Validating…' : 'Preview CSV'}</button>
        </div>
        <div className="grid gap-4 border-t border-slate-200 pt-4 md:grid-cols-2">
          <Field label="Resolve VM names in this tenant" hint="Only needed when the file uses vm_name instead of full resource ids.">
            <select value={connectionId} onChange={(event) => setConnectionId(event.target.value)} disabled={!canImport}>
              <option value="">No name resolution</option>
              {connections.data?.filter((item) => !item.disabled).map((item) => <option key={item.id} value={item.id}>{connectionOptionLabel(item)}</option>)}
            </select>
          </Field>
          {canReadGroups && <Field label="Destination when the file has no application column" hint="Rows without an application land here. Rows that name one are unaffected.">
            <select value={destinationId} onChange={(event) => setDestinationId(event.target.value)} disabled={!canImport}>
              <option value="">Use the application column</option>
              {groups.map((item) => <option key={item.id} value={item.id}>{'\u00a0'.repeat(item.depth * 2)}{item.name_path}</option>)}
            </select>
          </Field>}
        </div>
      </form>
      {!canImport && <p className="mt-3 text-sm text-amber-800">You need the <code className="font-mono">imports.write</code> permission to upload a CSV.</p>}
      {inspect.error && <div className="mt-4"><ErrorNotice error={inspect.error} /></div>}
      {result && <p className="mt-4 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">{result}</p>}
    </div>

    {!preview ? <EmptyState
      icon={<FileUp size={22} />}
      title="No file previewed yet"
      description="Choose a CSV to see a row-by-row validation report before anything is written to the database."
    /> : <section>
      <div className="card mb-4">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="text-sm text-slate-700"><strong>{preview.filename}</strong> — {preview.total} row{preview.total === 1 ? '' : 's'} · <span className="text-emerald-700">{preview.valid} valid</span> · <span className="text-rose-700">{preview.invalid} invalid</span></p>
            <div className="mt-2"><Summary preview={preview} /></div>
          </div>
          <div className="flex items-center gap-3">
            {preview.invalid > 0 && <label className="flex items-center gap-2 text-sm text-slate-600"><input className="!w-auto" type="checkbox" checked={invalidOnly} onChange={(event) => setInvalidOnly(event.target.checked)} />Invalid rows only</label>}
            <button className="btn-primary" disabled={!canImport || preview.invalid > 0 || !preview.valid || commit.isPending} onClick={() => commit.mutate()}>{commit.isPending ? 'Importing…' : `Import ${preview.total} row${preview.total === 1 ? '' : 's'}`}</button>
          </div>
        </div>
        {preview.invalid > 0 && <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">Atomic import is enabled: fix every invalid row and preview the file again. Nothing is created while any row fails validation.</p>}
        {commit.error && <div className="mt-3"><ErrorNotice error={commit.error} /></div>}
      </div>

      <ul className="space-y-2">{rows.map((row) => <RowCard key={row.row_number} row={row} />)}</ul>
    </section>}
  </>
}
