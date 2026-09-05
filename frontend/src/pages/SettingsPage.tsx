import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Clock, DatabaseBackup, Download, FlaskConical, Gauge, KeyRound, ShieldCheck, Upload } from 'lucide-react'
import { api, json } from '../api'
import { useAuth, useCan } from '../auth'
import { TimezonePicker } from '../components/TimezonePicker'
import { zoneLabel } from '../lib/time'
import { ErrorNotice, Field, Loading, PageHeader, formatDate } from '../components/Ui'
import { ConfirmDialog, Drawer } from '../components/Overlay'
import { Callout, SetupGuide, Step } from '../components/Help'
import type { BackupSection, DashboardData, DemoDataResult, DemoDataStatus, EstateResetResult, SettingsDocument, SettingsImportSummary } from '../types'

type Policy = {local_login_enabled:boolean;min_length:number;require_upper:boolean;require_lower:boolean;require_number:boolean;require_symbol:boolean;lockout_attempts:number;lockout_minutes:number;session_idle_minutes:number;session_absolute_hours:number;schedule_missed_grace_seconds:number}
type Settings = { app_name:string; environment:string; real_azure_starts_enabled:boolean; real_azure_stops_enabled:boolean; default_timezone:string; password_policy:Policy }
export function SettingsPage() {
  const query=useQuery({queryKey:['settings'],queryFn:()=>api<Settings>('/settings/general')}); const {refresh}=useAuth(); const canWriteSettings=useCan('settings.write'); const [message,setMessage]=useState('')
  const change=useMutation({mutationFn:(body:unknown)=>api('/auth/change-password',json('POST',body)),onSuccess:()=>{setMessage('Password updated.');void refresh()}})
  const submit=(event:FormEvent<HTMLFormElement>)=>{event.preventDefault();setMessage('');const form=new FormData(event.currentTarget);if(form.get('new_password')!==form.get('confirm')){setMessage('New passwords do not match.');return}change.mutate({current_password:form.get('current_password'),new_password:form.get('new_password')})}
    if(query.isLoading)return <Loading/>;if(query.error)return <ErrorNotice error={query.error}/>;const settings=query.data!
    return <><PageHeader title="Settings" description="Authentication, account security, and local application safety."/>
      <div className="grid gap-6 lg:grid-cols-2"><section className="card"><div className="flex items-center gap-3"><ShieldCheck className="text-blue-700"/><div><h2 className="font-semibold">General</h2><p className="muted">Runtime safety posture</p></div></div><dl className="mt-6 space-y-4 text-sm"><Row name="Application" value={settings.app_name}/><Row name="Environment" value={settings.environment}/><Row name="Global real starts" value={settings.real_azure_starts_enabled?'Enabled':'Disabled (mock mode)'}/><Row name="Global real stops" value={settings.real_azure_stops_enabled?'Enabled':'Disabled (mock mode)'}/></dl><div className={`mt-5 rounded-lg border p-3 text-sm ${settings.real_azure_starts_enabled||settings.real_azure_stops_enabled?'border-amber-200 bg-amber-50 text-amber-900':'border-emerald-200 bg-emerald-50 text-emerald-800'}`}>{settings.real_azure_starts_enabled||settings.real_azure_stops_enabled?'Starts and stops are gated separately — each also needs an enabled, writable tenant that permits that action.':'All scheduler executions use the deterministic mock adapter.'}</div></section>
      <section className="card"><div className="flex items-center gap-3"><KeyRound className="text-blue-700"/><div><h2 className="font-semibold">Change password</h2><p className="muted">At least {settings.password_policy.min_length} characters.</p></div></div><form className="mt-6 space-y-4" autoComplete="off" onSubmit={submit}>{change.error&&<ErrorNotice error={change.error}/>} {message&&<p className={`rounded-lg border p-3 text-sm ${message.includes('updated')?'border-emerald-200 bg-emerald-50 text-emerald-800':'border-rose-200 bg-rose-50 text-rose-800'}`}>{message}</p>}<Field label="Current password"><input name="current_password" type="password" autoComplete="new-password" required/></Field><Field label="New password"><input name="new_password" type="password" autoComplete="new-password" required/></Field><Field label="Confirm new password"><input name="confirm" type="password" autoComplete="new-password" required/></Field><button className="btn-primary" disabled={change.isPending}>Update password</button></form></section></div>
    <div className="mt-6 grid gap-6 lg:grid-cols-2"><DefaultTimezoneCard current={settings.default_timezone} canEdit={canWriteSettings}/><SchedulerTuningCard/></div>
    <BackupAndRestore/>
    <DemoDataCard/>
    <DangerZone/>
  </>
}

/** Application-wide default timezone. New schedules and CSV rows inherit it; existing schedules keep their own. */
function DefaultTimezoneCard({current,canEdit}:{current:string;canEdit:boolean}){
  const client=useQueryClient()
  const [zone,setZone]=useState(current)
  const save=useMutation({mutationFn:(value:string)=>api<{default_timezone:string}>('/settings/general',json('PUT',{default_timezone:value})),onSuccess:()=>{void client.invalidateQueries({queryKey:['settings']})}})
  return <section className="card">
    <Title icon={<Clock/>} title="Default timezone" subtitle={`Currently ${current} (${zoneLabel(current)})`}/>
    <div className="mt-5 space-y-4">
      {save.error&&<ErrorNotice error={save.error}/>}
      <Field label="Application default timezone"><TimezonePicker value={zone} onChange={setZone} disabled={!canEdit}/></Field>
      <Callout tone="info" title="What this changes">New schedules and imported CSV rows start out in this timezone. Changing it never rewrites an existing schedule — each schedule keeps the timezone it was created with, and every timestamp in the product is rendered with its zone label.</Callout>
      {canEdit
        ? <div className="flex items-center gap-3"><button type="button" className="btn-primary" disabled={zone===current||save.isPending} onClick={()=>save.mutate(zone)}>{save.isPending?'Saving…':'Save timezone'}</button>{save.isSuccess&&zone===current&&<span className="text-sm text-emerald-700">Saved.</span>}</div>
        : <p className="text-sm text-slate-600">Only administrators can change the default timezone.</p>}
    </div>
  </section>
}

const SCHEDULER_TUNING = [
  {name:'SCHEDULER_CLAIM_BATCH',value:'50',desc:'Schedules leased per scheduler poll before work is dispatched.'},
  {name:'SCHEDULER_START_CONCURRENCY',value:'12',desc:'Concurrent VM start calls in flight across all runs.'},
  {name:'SCHEDULER_MONITOR_CONCURRENCY',value:'40',desc:'Concurrent power-state polls while runs are being monitored.'},
  {name:'AZURE_SUBSCRIPTION_CONCURRENCY',value:'8',desc:'Per-subscription cap, so one subscription cannot exhaust the pool.'},
  {name:'SCHEDULER_POLL_SECONDS',value:'15',desc:'Interval between scheduler polls for due schedules.'},
] as const

/** Scheduler tuning is environment-only, so it is surfaced read-only with its defaults. */
function SchedulerTuningCard(){
  return <section className="card">
    <Title icon={<Gauge/>} title="Scheduler tuning" subtitle="Environment-only — restart the backend to apply"/>
    <div className="mt-5 overflow-x-auto rounded-lg border border-slate-200">
      <table className="w-full text-left text-xs">
        <thead className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-500"><tr><th className="px-3 py-2 font-semibold">Environment variable</th><th className="px-3 py-2 font-semibold">Default</th><th className="px-3 py-2 font-semibold">Effect</th></tr></thead>
        <tbody className="divide-y divide-slate-200">{SCHEDULER_TUNING.map(row=><tr key={row.name}><td className="whitespace-nowrap px-3 py-2 font-mono text-slate-800">{row.name}</td><td className="whitespace-nowrap px-3 py-2 tabular-nums text-slate-600">{row.value}</td><td className="px-3 py-2 text-slate-600">{row.desc}</td></tr>)}</tbody>
      </table>
    </div>
    <p className="mt-3 text-xs text-slate-500">These values are read from the backend environment at start-up and are not editable from the browser. Production scheduling stays single-replica while SQLite and the in-process scheduler are used.</p>
  </section>
}

function Title({icon,title,subtitle}:{icon:React.ReactNode;title:string;subtitle:string}){return <div className="flex items-center gap-3"><span className="text-blue-700">{icon}</span><div><h2 className="font-semibold">{title}</h2><p className="muted">{subtitle}</p></div></div>}
function Row({name,value}:{name:string;value:string}){return <div className="flex justify-between gap-4 border-b border-slate-200 pb-3"><dt className="text-slate-600">{name}</dt><dd className="font-medium">{value}</dd></div>}

// -- backup & restore --------------------------------------------------

const IMPORT_SECTIONS: readonly {id:BackupSection;label:string;hint:string}[] = [
  {id:'azure_connections',label:'Azure tenants',hint:'Tenant, client and safety flags — credentials are never in the file'},
  {id:'connectors',label:'Notification connectors',hint:'Type, mode and non-secret configuration'},
  {id:'groups',label:'Applications & rings',hint:'The whole tree, matched by its name path'},
  {id:'virtual_machines',label:'Virtual machines',hint:'Matched by Azure resource id'},
  {id:'schedules',label:'Schedules',hint:'Matched by target, type and start time'},
  {id:'notification_rules',label:'Notification rules',hint:'Connectors and scope resolved by name'},
  {id:'security_policy',label:'Password & session policy',hint:'Overwrites the current policy'},
  {id:'identity_provider',label:'Entra ID settings',hint:'Tenant and client id only; the secret must be re-entered'},
  {id:'roles',label:'Custom roles',hint:'Built-in roles are owned by the app and are never imported'},
  {id:'access_groups',label:'Access groups',hint:'Their roles are resolved by name'},
] as const

const ALL_SECTIONS = IMPORT_SECTIONS.map((item)=>item.id)

async function downloadExport(){
  const response = await fetch('/api/admin/export',{credentials:'include',headers:{Accept:'application/json'}})
  if(!response.ok) throw new Error(`Export failed (${response.status})`)
  const blob = await response.blob()
  const suggested = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition')??'')?.[1]
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = suggested ?? `azure-vm-scheduler-settings-${new Date().toISOString().slice(0,10)}.json`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

/** Export the portable settings document and restore one, in merge or replace mode, behind a preview. */
function BackupAndRestore(){
  const canManage=useCan('backup.manage')
  const client=useQueryClient()
  const [open,setOpen]=useState(false)
  const [document_,setDocument]=useState<SettingsDocument|null>(null)
  const [filename,setFilename]=useState('')
  const [parseError,setParseError]=useState('')
  const [mode,setMode]=useState<'merge'|'replace'>('merge')
  const [sections,setSections]=useState<BackupSection[]>([...ALL_SECTIONS])
  const [preview,setPreview]=useState<SettingsImportSummary|null>(null)
  const [result,setResult]=useState<SettingsImportSummary|null>(null)
  const [confirmReplace,setConfirmReplace]=useState(false)
  const exportNow=useMutation({mutationFn:downloadExport})
  const previewImport=useMutation({mutationFn:()=>api<SettingsImportSummary>('/admin/import/preview',json('POST',{document:document_,mode,sections})),onSuccess:(data)=>{setPreview(data);setResult(null)}})
  const runImport=useMutation({
    mutationFn:()=>api<SettingsImportSummary>('/admin/import',json('POST',{document:document_,mode,sections})),
    onSuccess:(data)=>{setResult(data);setPreview(null);['groups','vms','schedules','dashboard','connections','connectors','notification-rules','admin-auth','settings'].forEach((key)=>void client.invalidateQueries({queryKey:[key]}))},
  })
  if(!canManage) return null
  const reset=()=>{setDocument(null);setFilename('');setParseError('');setPreview(null);setResult(null);previewImport.reset();runImport.reset()}
  const pickFile=async(file:File|undefined)=>{
    reset()
    if(!file) return
    setFilename(file.name)
    try{
      const parsed=JSON.parse(await file.text()) as SettingsDocument
      if(parsed?.format!=='azure-vm-scheduler.settings'&&parsed?.format!=='azureops.settings') throw new Error('This file is not an Azure VM Scheduler settings export.')
      setDocument(parsed)
    }catch(error){setParseError(error instanceof Error?error.message:'The file could not be read as JSON.')}
  }
  const toggleSection=(id:BackupSection)=>setSections((current)=>current.includes(id)?current.filter((item)=>item!==id):[...current,id])
  const start=()=>{if(mode==='replace'){setConfirmReplace(true);return}runImport.mutate()}
  const counts=(source:SettingsDocument|null,key:string)=>Array.isArray(source?.[key])?(source[key] as unknown[]).length:0
  return <section className="card mt-6">
    <Title icon={<DatabaseBackup/>} title="Backup & restore" subtitle="Export every setting to a file, or restore one into this install"/>
    <div className="mt-5 space-y-4">
      {exportNow.error&&<ErrorNotice error={exportNow.error}/>}
      <div className="flex flex-wrap gap-3">
        <button type="button" className="btn-primary inline-flex items-center gap-2" disabled={exportNow.isPending} onClick={()=>exportNow.mutate()}><Download size={16}/>{exportNow.isPending?'Preparing…':'Export settings'}</button>
        <button type="button" className="btn-secondary inline-flex items-center gap-2" onClick={()=>{reset();setOpen(true)}}><Upload size={16}/>Import settings</button>
      </div>
      <BackupGuide/>
    </div>

    <Drawer open={open} title="Import settings" description={filename||'Choose an Azure VM Scheduler settings export'} width="max-w-2xl" onClose={()=>setOpen(false)}
      footer={<>
        <button type="button" className="btn-secondary" onClick={()=>setOpen(false)}>Close</button>
        <button type="button" className="btn-secondary" disabled={!document_||!sections.length||previewImport.isPending} onClick={()=>previewImport.mutate()}>{previewImport.isPending?'Checking…':'Preview'}</button>
        <button type="button" className={mode==='replace'?'btn-danger':'btn-primary'} disabled={!document_||!preview||!sections.length||runImport.isPending} onClick={start}>{runImport.isPending?'Importing…':mode==='replace'?'Replace and import':'Import'}</button>
      </>}>
      <div className="space-y-5">
        <Field label="Settings file"><input type="file" accept="application/json,.json" onChange={(event)=>void pickFile(event.target.files?.[0])}/></Field>
        {parseError&&<div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{parseError}</div>}
        {document_&&<Callout tone="neutral" title="Document">Version {String(document_.version)} exported {formatDate(String(document_.exported_at))} from Azure VM Scheduler {String(document_.app_version||'unknown')} · {counts(document_,'groups')} groups · {counts(document_,'virtual_machines')} VMs · {counts(document_,'schedules')} schedules</Callout>}

        <div className="field"><label>Mode</label>
          <div className="space-y-2">
            <label className="flex gap-3 rounded-lg border border-slate-200 p-3 text-sm"><input className="!w-auto mt-0.5" type="radio" name="import-mode" checked={mode==='merge'} onChange={()=>{setMode('merge');setPreview(null)}}/><span><strong>Merge</strong> — create anything that is missing and leave everything that already exists untouched. Nothing is ever deleted.</span></label>
            <label className="flex gap-3 rounded-lg border border-rose-200 bg-rose-50/40 p-3 text-sm"><input className="!w-auto mt-0.5" type="radio" name="import-mode" checked={mode==='replace'} onChange={()=>{setMode('replace');setPreview(null)}}/><span><strong>Replace</strong> — delete every application, ring, virtual machine, schedule and run first, then import. Users, sessions and the audit log are kept.</span></label>
          </div>
        </div>

        <div className="field"><label>Sections</label>
          <div className="grid gap-1.5 sm:grid-cols-2">{IMPORT_SECTIONS.map((item)=><label key={item.id} className="flex gap-2 rounded border border-slate-200 p-2 text-xs"><input className="!w-auto mt-0.5" type="checkbox" checked={sections.includes(item.id)} onChange={()=>{toggleSection(item.id);setPreview(null)}}/><span><span className="font-medium text-slate-800">{item.label}</span><br/><span className="text-slate-500">{item.hint}</span></span></label>)}</div>
        </div>

        {previewImport.error&&<ErrorNotice error={previewImport.error}/>}
        {runImport.error&&<ErrorNotice error={runImport.error}/>}
        {preview&&<ImportSummary summary={preview} heading="Preview — nothing has been written yet"/>}
        {result&&<ImportSummary summary={result} heading="Import complete"/>}
      </div>
    </Drawer>

    <ConfirmDialog open={confirmReplace} title="Replace the current estate?" confirmLabel="Replace and import" busy={runImport.isPending} onCancel={()=>setConfirmReplace(false)} onConfirm={()=>{setConfirmReplace(false);runImport.mutate()}}>
      <p>Every application, ring, virtual machine, schedule, run and start attempt in this install is deleted first, then the file is imported.</p>
      <p className="mt-2">Users, sessions, the audit log, Azure tenants, connectors and notification rules are <strong>not</strong> deleted. This cannot be undone.</p>
    </ConfirmDialog>
  </section>
}

function ImportSummary({summary,heading}:{summary:SettingsImportSummary;heading:string}){
  const rows=IMPORT_SECTIONS.map((item)=>({...item,data:summary.sections[item.id]})).filter((item)=>item.data)
  return <div className="space-y-3">
    <Callout tone={summary.failed?'warn':'success'} title={heading}>{summary.created} created · {summary.skipped} already present · {summary.failed} failed{summary.removed?.groups_removed?` · ${summary.removed.groups_removed} groups removed first`:''}</Callout>
    {!!summary.needs_secret.length&&<Callout tone="warn" title="Secrets must be re-entered"><ul className="list-disc space-y-1 pl-4">{summary.needs_secret.map((item)=><li key={item}>{item}</li>)}</ul></Callout>}
    <div className="overflow-x-auto rounded-lg border border-slate-200">
      <table className="w-full text-left text-xs">
        <thead className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-500"><tr><th className="px-3 py-2 font-semibold">Section</th><th className="px-3 py-2 font-semibold">Created</th><th className="px-3 py-2 font-semibold">Skipped</th><th className="px-3 py-2 font-semibold">Failed</th></tr></thead>
        <tbody className="divide-y divide-slate-200">{rows.map((row)=><tr key={row.id}><td className="px-3 py-2 text-slate-800">{row.label}</td><td className="px-3 py-2 tabular-nums text-emerald-700">{row.data!.created}</td><td className="px-3 py-2 tabular-nums text-slate-500">{row.data!.skipped}</td><td className={`px-3 py-2 tabular-nums ${row.data!.failed?'text-rose-700':'text-slate-500'}`}>{row.data!.failed}</td></tr>)}</tbody>
      </table>
    </div>
    {rows.filter((row)=>row.data!.failed).map((row)=><Callout key={row.id} tone="warn" title={`${row.label} — problems`}><ul className="list-disc space-y-1 pl-4">{row.data!.details.filter((item)=>item.outcome==='failed').map((item,index)=><li key={index}>{item.message}</li>)}</ul></Callout>)}
  </div>
}

function BackupGuide(){
  return <SetupGuide title="What a settings export does and does not contain" subtitle="Read before restoring">
    <Callout tone="warn" title="Secrets are never exported">No client secret, certificate, pasted Azure token, SMTP password, ServiceNow password, webhook URL, signing secret or Entra client secret is written to the file — not in plaintext and not encrypted. Each Azure tenant and connector instead carries a list of the fields you must re-enter. A connector or tenant that needs a secret is created <strong>disabled</strong> so it can never fire half-configured.</Callout>
    <ol className="mt-4 space-y-4">
      <Step n={1} title="Export">Downloads a single JSON document containing the application/ring tree, virtual machines, schedules, Azure tenant definitions, connectors, notification rules, the password &amp; session policy and the Entra tenant/client ids.</Step>
      <Step n={2} title="What is deliberately left out">Users, sessions, the audit log, schedule runs, VM attempts, notification events and deliveries, and CSV import batches. Those are operational history or credentials and are never portable.</Step>
      <Step n={3} title="Everything is referenced by name">Groups are stored as a root-to-leaf name path, virtual machines by Azure resource id, and tenants and connectors by their display name, so a document restores cleanly into a different database.</Step>
      <Step n={4} title="Preview first">The preview runs the whole import inside a transaction that is rolled back, so you see the exact created / already-present / failed counts per section before anything is written.</Step>
      <Step n={5} title="Choose merge or replace">Merge only adds what is missing and never deletes. Replace removes the entire estate first — applications, rings, virtual machines, schedules and runs — and keeps users, sessions and audit history.</Step>
      <Step n={6} title="Finish the credentials">After importing, open <strong>Azure tenants</strong> and <strong>Connectors</strong>, re-enter each listed secret, then enable the connection or connector.</Step>
    </ol>
  </SetupGuide>
}

// -- demo data ---------------------------------------------------------

/**
 * Sample estate for demos and for finding your way around a fresh install. Removal is driven by a
 * flag the loader sets, so it only ever deletes what it created — a real application named Zava
 * is skipped on load and survives removal.
 */
function DemoDataCard(){
  const canEdit=useCan('groups.write')
  const client=useQueryClient()
  const [confirming,setConfirming]=useState(false)
  const [note,setNote]=useState('')
  const status=useQuery({queryKey:['demo-data'],queryFn:()=>api<DemoDataStatus>('/admin/demo-data'),enabled:canEdit})
  const refreshEstate=()=>['groups','vms','schedules','dashboard','overview','runs','timeline','demo-data'].forEach((key)=>void client.invalidateQueries({queryKey:[key]}))
  const load=useMutation({
    mutationFn:()=>api<DemoDataResult>('/admin/demo-data',json('POST',{})),
    onSuccess:(data)=>{setNote(data.applications?`Added ${data.applications} applications, ${data.rings} rings, ${data.virtual_machines} virtual machines and ${data.schedules} schedules.`:'Nothing to add — the sample applications are already present.');refreshEstate()},
  })
  const remove=useMutation({
    mutationFn:()=>api<DemoDataResult>('/admin/demo-data',{method:'DELETE'}),
    onSuccess:(data)=>{setNote(`Removed ${data.applications} applications, ${data.rings} rings, ${data.virtual_machines} virtual machines, ${data.schedules} schedules and ${data.runs??0} runs.`);setConfirming(false);refreshEstate()},
  })
  if(!canEdit) return null
  const loaded=status.data?.loaded??false
  const busy=load.isPending||remove.isPending
  return <section className="card mt-6">
    <Title icon={<FlaskConical/>} title="Demo data" subtitle="A sample Zava estate to explore the app with"/>
    <div className="mt-5 space-y-4">
      {load.error&&<ErrorNotice error={load.error}/>}
      {remove.error&&<ErrorNotice error={remove.error}/>}
      {note&&<Callout tone="success" title="Done">{note}</Callout>}
      <p className="text-sm text-slate-600">Four Zava applications with their rings, virtual machines and start/stop waves. The machines are not real Azure resources, so every run against them stays in mock mode.</p>
      {loaded
        ? <Callout tone="info" title="Sample data is loaded">{status.data?.applications} applications, {status.data?.rings} rings, {status.data?.virtual_machines} virtual machines and {status.data?.schedules} schedules.</Callout>
        : <p className="text-sm text-slate-500">Not loaded.</p>}
      <div className="flex flex-wrap gap-3">
        <button type="button" className="btn-primary" disabled={busy||loaded} onClick={()=>{setNote('');load.mutate()}}>{load.isPending?'Loading…':'Load demo data'}</button>
        <button type="button" className="btn-danger" disabled={busy||!loaded} onClick={()=>{setNote('');setConfirming(true)}}>Remove demo data</button>
      </div>
    </div>

    <ConfirmDialog open={confirming} title="Remove the demo data?" confirmLabel="Remove demo data" busy={remove.isPending} onCancel={()=>setConfirming(false)} onConfirm={()=>remove.mutate()}>
      <p>This deletes the sample Zava applications and everything inside them:</p>
      <ul className="mt-2 list-disc space-y-1 pl-5">
        <li><strong>{status.data?.applications??0}</strong> applications and <strong>{status.data?.rings??0}</strong> rings</li>
        <li><strong>{status.data?.virtual_machines??0}</strong> virtual machines</li>
        <li><strong>{status.data?.schedules??0}</strong> schedules and their run history</li>
      </ul>
      <p className="mt-3">Your own applications are not affected.</p>
    </ConfirmDialog>
  </section>
}


// -- danger zone -------------------------------------------------------

/** Irreversible removal of the whole schedulable estate. Identity, audit and credentials survive. */
function DangerZone(){
  const canManage=useCan('backup.manage')
  const canReadDashboard=useCan('dashboard.read')
  const client=useQueryClient()
  const [open,setOpen]=useState(false)
  const [typed,setTyped]=useState('')
  const [removed,setRemoved]=useState<EstateResetResult|null>(null)
  const dashboard=useQuery({queryKey:['dashboard'],queryFn:()=>api<DashboardData>('/dashboard'),enabled:canManage&&canReadDashboard})
  const reset=useMutation({
    mutationFn:()=>api<EstateResetResult>('/admin/reset-estate',json('POST',{confirm:'DELETE'})),
    onSuccess:(data)=>{setRemoved(data);setOpen(false);setTyped('');['groups','vms','schedules','dashboard','runs','timeline'].forEach((key)=>void client.invalidateQueries({queryKey:[key]}))},
  })
  if(!canManage) return null
  const counts=dashboard.data
  return <section className="card mt-6 border-rose-300 bg-rose-50/30">
    <Title icon={<AlertTriangle className="text-rose-600"/>} title="Danger zone" subtitle="Irreversible removal of the whole estate"/>
    <div className="mt-5 space-y-4">
      {reset.error&&<ErrorNotice error={reset.error}/>}
      {removed&&<Callout tone="success" title="Estate deleted">{removed.groups_removed} applications and rings, {removed.vms_removed} virtual machines, {removed.schedules_removed} schedules and {removed.runs_removed} runs were removed.</Callout>}
      <Callout tone="warn" title="What this deletes">Every application, ring, virtual machine, schedule, run and start attempt. Users, sessions, the audit log, Azure tenants, connectors, notification rules and the security policy are left untouched. Export your settings first if you may want them back.</Callout>
      <button type="button" className="btn-danger" onClick={()=>{setRemoved(null);setTyped('');setOpen(true)}}>Delete all applications, virtual machines and schedules</button>
    </div>

    <ConfirmDialog open={open} title="Delete the entire estate?" confirmLabel="Delete everything" confirmDisabled={typed!=='DELETE'} busy={reset.isPending} onCancel={()=>{setOpen(false);setTyped('')}} onConfirm={()=>{if(typed==='DELETE')reset.mutate()}}>
      <p>This will permanently remove:</p>
      <ul className="mt-2 list-disc space-y-1 pl-5">
        <li><strong>{counts?.application_count??'…'}</strong> applications and <strong>{counts?.ring_count??'…'}</strong> rings</li>
        <li><strong>{counts?.vm_count??'…'}</strong> virtual machines</li>
        <li><strong>{counts?.schedule_count??'…'}</strong> schedules and every recorded run</li>
      </ul>
      <p className="mt-3">Type <code className="font-mono font-semibold">DELETE</code> to enable the button.</p>
      <input className="mt-2" autoComplete="off" value={typed} onChange={(event)=>setTyped(event.target.value)} placeholder="DELETE" aria-label="Type DELETE to confirm"/>
      {typed!=='DELETE'&&<p className="mt-2 text-xs text-slate-500">The button stays disabled until the confirmation matches exactly.</p>}
      {typed==='DELETE'&&<p className="mt-2 text-xs text-rose-700">This cannot be undone.</p>}
    </ConfirmDialog>
  </section>
}
