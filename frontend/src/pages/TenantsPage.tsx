import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Cloud, Pencil, Plus, RefreshCw, Star, Trash2, X } from 'lucide-react'
import { api, json } from '../api'
import { StatusBadge } from '../components/StatusBadge'
import { Empty, ErrorNotice, Field, Loading, PageHeader } from '../components/Ui'
import { Callout, CmdBlock, PermissionTable, SetupGuide, Step } from '../components/Help'
import type { Connection } from '../types'

type FormState = Partial<Connection> & {client_secret?:string;certificate_pem?:string;access_token_json?:string}
const blank:FormState={display_name:'',tenant_id:'',auth_method:'azure_cli',allow_vm_start:false,allow_vm_stop:false,read_only:false,is_default:false,disabled:false}
export function TenantsPage(){
  const client=useQueryClient();const [form,setForm]=useState<FormState>();const [notice,setNotice]=useState('');const [discovered,setDiscovered]=useState<Record<string,{id:string;name:string}[]>>({})
  const query=useQuery({queryKey:['connections'],queryFn:()=>api<Connection[]>('/connections')});const refresh=()=>client.invalidateQueries({queryKey:['connections']})
  const save=useMutation({mutationFn:(body:FormState)=>api<Connection>('/connections',json('PUT',body)),onSuccess:()=>{setForm(undefined);setNotice('Tenant connection saved.');void refresh()}})
  const remove=useMutation({mutationFn:(id:string)=>api(`/connections/${id}`,json('DELETE')),onSuccess:()=>void refresh()})
  const makeDefault=useMutation({mutationFn:(id:string)=>api(`/connections/${id}/default`,json('POST')),onSuccess:()=>void refresh()})
  const test=useMutation({mutationFn:(id:string)=>api<{subscriptions:{id:string;name:string}[]}>(`/connections/${id}/test`,json('POST')),onSuccess:(data,id)=>{setDiscovered(current=>({...current,[id]:data.subscriptions}));setNotice(`Connected. ${data.subscriptions.length} subscription(s) visible.`);void refresh()}})
  const discover=useMutation({mutationFn:(id:string)=>api<{subscriptions:{id:string;name:string}[]}>(`/connections/${id}/discover`,json('POST')),onSuccess:(data,id)=>setDiscovered(current=>({...current,[id]:data.subscriptions}))})
  const submit=(event:FormEvent<HTMLFormElement>)=>{event.preventDefault();save.mutate(form!)}
  const method=form?.auth_method
  return <><PageHeader title="Azure Tenants" description="Encrypted local credential registry for Azure subscriptions." action={<button className="btn-primary" onClick={()=>setForm(form?undefined:blank)}>{form?<X size={17}/>:<Plus size={17}/>} {form?'Close':'Connect tenant'}</button>}/>
    <div className="mb-5 rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-900"><strong>Two safety gates per action:</strong> a real start needs the server-wide starts flag and this tenant&apos;s <em>Allow VM starts</em>; a real stop needs the server-wide stops flag and <em>Allow VM stops</em>. The pairs are independent, so enabling starts never enables stops. Withdrawing a permission takes effect on the next virtual machine. Credentials are encrypted with the host&apos;s stable Fernet key.</div>
    <div className="mb-5"><TenantSetupGuide/></div>
    {notice&&<p className="mb-4 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">{notice}</p>}
    {(save.error||test.error||discover.error||remove.error)&&<div className="mb-4"><ErrorNotice error={save.error||test.error||discover.error||remove.error}/></div>}
    {form&&<div className="mb-3 rounded-lg border border-slate-200 bg-white p-3"><Toggle label="Read-only connection (block all real VM starts and stops)" checked={!!form.read_only} onChange={value=>setForm({...form,read_only:value})}/></div>}
    {form&&<form className="card mb-6" autoComplete="off" onSubmit={submit}><h2 className="mb-5 font-semibold">{form.id?'Edit tenant':'New tenant connection'}</h2><div className="grid gap-4 md:grid-cols-2"><Field label="Display name"><input value={form.display_name??''} onChange={e=>setForm({...form,display_name:e.target.value})} required/></Field><Field label="Authentication"><select value={method} onChange={e=>setForm({...form,auth_method:e.target.value})}><option value="azure_cli">Azure CLI session</option><option value="default_chain">Default credential chain</option><option value="service_principal">Service principal secret</option><option value="service_principal_cert">Service principal certificate</option><option value="az_cli_token">Pasted Azure CLI token</option></select></Field><div className="md:col-span-2"><AuthMethodHelp method={method} tenantId={form.tenant_id}/></div>{method!=='az_cli_token'&&<Field label="Tenant ID"><input value={form.tenant_id??''} onChange={e=>setForm({...form,tenant_id:e.target.value})} placeholder="Directory (tenant) UUID"/></Field>}{(method==='service_principal'||method==='service_principal_cert')&&<Field label="Client ID"><input value={form.client_id??''} onChange={e=>setForm({...form,client_id:e.target.value})}/></Field>}{method==='service_principal'&&<Field label="Client secret"><input type="password" value={form.client_secret??''} onChange={e=>setForm({...form,client_secret:e.target.value})} placeholder={form.id?'Leave blank to keep existing':'Required'}/></Field>}{method==='service_principal_cert'&&<Field label="Certificate PEM" wide><textarea rows={4} value={form.certificate_pem??''} onChange={e=>setForm({...form,certificate_pem:e.target.value})} placeholder={form.id?'Leave blank to keep existing':'-----BEGIN CERTIFICATE-----'}/></Field>}{method==='az_cli_token'&&<Field label="Access token or az JSON" wide><textarea rows={4} value={form.access_token_json??''} onChange={e=>setForm({...form,access_token_json:e.target.value})} placeholder={form.id?'Leave blank to keep existing':'Paste az account get-access-token output'}/></Field>}<Field label="Default subscription"><input value={form.default_subscription??''} onChange={e=>setForm({...form,default_subscription:e.target.value})} placeholder="Optional subscription UUID"/></Field><div className="space-y-3 md:col-span-2"><Toggle label="Allow VM starts for this tenant" checked={!!form.allow_vm_start} onChange={value=>setForm({...form,allow_vm_start:value})}/><Toggle label="Allow VM stops for this tenant" checked={!!form.allow_vm_stop} onChange={value=>setForm({...form,allow_vm_stop:value})}/><Toggle label="Make default tenant" checked={!!form.is_default} onChange={value=>setForm({...form,is_default:value})}/><Toggle label="Connection disabled" checked={!!form.disabled} onChange={value=>setForm({...form,disabled:value})}/></div></div><div className="mt-5 flex justify-end"><button className="btn-primary" disabled={save.isPending}>{save.isPending?'Saving…':'Save connection'}</button></div></form>}
    {query.isLoading?<Loading/>:query.error?<ErrorNotice error={query.error}/>:!query.data?.length?<Empty>No Azure tenant connections. Scheduler executions will remain mocked.</Empty>:<div className="grid gap-4 xl:grid-cols-2">{query.data.map(item=><article className="card" key={item.id}><div className="flex items-start justify-between gap-3"><div className="flex gap-3"><span className="grid h-10 w-10 place-items-center rounded-lg bg-sky-100 text-blue-700"><Cloud size={20}/></span><div><div className="flex flex-wrap items-center gap-2"><h2 className="font-semibold">{item.display_name}</h2>{item.is_default&&<span className="text-amber-600"><Star size={15} fill="currentColor"/></span>}<StatusBadge value={item.disabled?'disabled':item.status}/></div><p className="text-xs text-slate-500">{item.auth_method.replaceAll('_',' ')} · {item.tenant_id||'tenant from token'}</p></div></div><button className="btn-secondary !p-2" onClick={()=>setForm({...item})} aria-label="Edit"><Pencil size={16}/></button></div><div className="mt-4 grid grid-cols-2 gap-3 text-xs"><Info label="VM starts" value={item.allow_vm_start?'Allowed':'Blocked'}/><Info label="VM stops" value={item.allow_vm_stop?'Allowed':'Blocked'}/><Info label="Credentials" value={item.has_client_secret||item.has_certificate_pem||item.has_access_token_json?'Stored encrypted':'Host identity'}/></div>{item.status_detail&&<p className="mt-3 text-xs text-slate-600">{item.status_detail}</p>}{discovered[item.id]&&<select className="mt-3" defaultValue=""><option value="">{discovered[item.id].length} discovered subscription(s)</option>{discovered[item.id].map(sub=><option value={sub.id} key={sub.id}>{sub.name} — {sub.id}</option>)}</select>}<div className="mt-4 flex flex-wrap gap-2"><button className="btn-secondary" onClick={()=>test.mutate(item.id)} disabled={test.isPending}><Check size={15}/>Test</button><button className="btn-secondary" onClick={()=>discover.mutate(item.id)} disabled={discover.isPending}><RefreshCw size={15}/>Discover</button>{!item.is_default&&<button className="btn-secondary" onClick={()=>makeDefault.mutate(item.id)}><Star size={15}/>Default</button>}<button className="btn-danger ml-auto" onClick={()=>{if(confirm(`Delete ${item.display_name}?`))remove.mutate(item.id)}}><Trash2 size={15}/></button></div></article>)}</div>}
  </>
}

const VM_START_ACTIONS = [
  {name:'Microsoft.Compute/virtualMachines/read',type:'Action',desc:'Resolve the VM from its resource ID before a scheduled start.'},
  {name:'Microsoft.Compute/virtualMachines/instanceView/read',type:'Action',desc:'Read the live power state to confirm the VM actually reached running.'},
  {name:'Microsoft.Compute/virtualMachines/start/action',type:'Action',desc:'Perform the start operation itself.'},
  {name:'Microsoft.Resources/subscriptions/read',type:'Action',desc:'List visible subscriptions for Test and Discover.'},
] as const

function TenantSetupGuide(){
  return <SetupGuide title="Setup guide — connect an Azure tenant" subtitle="5 authentication methods">
    <Callout tone="info">Pick the method that matches where Azure VM Scheduler runs. For local development the <strong>Azure CLI session</strong> is easiest and never stores a credential. For unattended scheduling use a <strong>service principal</strong> or, once hosted in Azure, the <strong>default credential chain</strong> with a managed identity.</Callout>
    <ol className="mt-4 space-y-4">
      <Step n={1} title="Choose an authentication method">
        <ul className="list-disc space-y-1 pl-4">
          <li><strong>Azure CLI session</strong> — reuses <code className="font-mono">az login</code> on this host. Refreshes itself, nothing is stored. Best for local use.</li>
          <li><strong>Default credential chain</strong> — managed identity in Azure, or environment / CLI credentials locally. Best once deployed.</li>
          <li><strong>Service principal secret</strong> — tenant ID, client ID and a client secret. Best for unattended runs.</li>
          <li><strong>Service principal certificate</strong> — same, with a PEM key pair instead of a secret. Best where secrets are prohibited.</li>
          <li><strong>Pasted Azure CLI token</strong> — a one-hour token for a quick test. Not suitable for schedules.</li>
        </ul>
      </Step>
      <Step n={2} title="Grant the identity access to the VMs">Assign a role at the narrowest scope that covers the VMs you schedule — a single VM, a resource group, or a subscription. The built-in <strong>Virtual Machine Contributor</strong> role is sufficient; a custom role with only the actions below is tighter.<PermissionTable rows={VM_START_ACTIONS}/><p className="mt-2">Example assignment for a service principal scoped to one resource group:</p><CmdBlock cmd={'az role assignment create --assignee <CLIENT_ID> --role "Virtual Machine Contributor" --scope /subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>'}/></Step>
      <Step n={3} title="Create the connection">Click <strong>Connect tenant</strong>, give it a display name, select the method and fill the credential fields. Method-specific instructions appear inside the form.</Step>
      <Step n={4} title="Test and discover">Save, then use <strong>Test</strong> to verify authentication and <strong>Discover</strong> to list visible subscriptions and pick a default subscription. Both actions only contact Azure when you click them.</Step>
      <Step n={5} title="Allow VM starts">A connection is safe by default: leave <em>Read-only</em> on to permit testing and discovery but block starts. Turn on <strong>Allow VM starts</strong> only when you want this tenant to perform real starts — the server-wide <code className="font-mono">ENABLE_REAL_AZURE_STARTS</code> flag must also be enabled, otherwise the scheduler keeps using the mock adapter.</Step>
    </ol>
    <div className="mt-4 grid gap-3 lg:grid-cols-2">
      <Callout tone="neutral" title="Create a service principal">
        <p>Run this once, then copy <code className="font-mono">appId</code> into Client ID, <code className="font-mono">password</code> into Client secret and <code className="font-mono">tenant</code> into Tenant ID:</p>
        <CmdBlock cmd={'az ad sp create-for-rbac --name azureops-scheduler --role "Virtual Machine Contributor" --scopes /subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>'}/>
        <p className="mt-2">The password is shown once. Record its expiry and rotate it before it lapses — an expired secret makes scheduled starts fail.</p>
      </Callout>
      <Callout tone="warn" title="Troubleshooting">
        <ul className="list-disc space-y-1 pl-4">
          <li><strong>AuthorizationFailed</strong> — the identity has no role assignment covering that VM scope.</li>
          <li><strong>Please run &apos;az login&apos;</strong> — no CLI session exists for the account running the backend, or a different tenant is signed in.</li>
          <li><strong>0 subscriptions visible</strong> — authentication worked but the identity has no reader access anywhere; assign a role.</li>
          <li><strong>Token expired</strong> — pasted CLI tokens last about an hour. Re-paste, or switch to a persistent method.</li>
          <li><strong>Starts stay mocked</strong> — check both the tenant&apos;s <em>Allow VM starts</em> toggle and the server-wide flag on the Settings page.</li>
          <li><strong>Stops stay mocked</strong> — stops are gated separately, so enabling starts never enables stops. Check <em>Allow VM stops</em> here and the server-wide stop flag on Settings.</li>
        </ul>
      </Callout>
    </div>
  </SetupGuide>
}

function AuthMethodHelp({method,tenantId}:{method?:string;tenantId?:string}){
  const tenant=tenantId||'<TENANT_ID>'
  if(method==='azure_cli')return <Callout tone="success" title="Sign in once — stays connected">
    <p>Uses the Azure CLI session belonging to the account that runs this backend. The CLI keeps the session refreshed automatically, so no token is pasted and it does not expire after an hour.</p>
    <ol className="mt-2 list-decimal space-y-1 pl-4">
      <li>On the machine running Azure VM Scheduler, sign in to the tenant:<CmdBlock cmd={`az login --tenant ${tenant}`}/></li>
      <li>Confirm the CLI can see the subscription you schedule against:<CmdBlock cmd="az account show --output table"/></li>
      <li>Enter the Tenant ID above, save, then use <strong>Test</strong>. There is nothing else to paste.</li>
    </ol>
    <p className="mt-2">Requires the Azure CLI to be installed on this host. Re-authentication is only needed if you sign out or the session is revoked. For a fully unattended service, use a service principal instead.</p>
  </Callout>
  if(method==='default_chain')return <Callout tone="neutral" title="Host identity (DefaultAzureCredential)">
    <p>Tries managed identity first, then environment variables, then the Azure CLI session. This is the method to use once Azure VM Scheduler runs in Azure Container Apps with a managed identity assigned — no credential is ever stored by the application.</p>
    <p className="mt-2">Locally it falls back to your <code className="font-mono">az login</code> session. To pin a user-assigned managed identity, set <code className="font-mono">AZURE_CLIENT_ID</code> in the backend environment.</p>
  </Callout>
  if(method==='service_principal')return <Callout tone="info" title="Service principal with a client secret">
    <p>Best for unattended scheduling. Create the app registration and role assignment in one step:</p>
    <CmdBlock cmd={'az ad sp create-for-rbac --name azureops-scheduler --role "Virtual Machine Contributor" --scopes /subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>'}/>
    <p className="mt-2">Map the output to this form: <code className="font-mono">tenant</code> → Tenant ID, <code className="font-mono">appId</code> → Client ID, <code className="font-mono">password</code> → Client secret. The secret is Fernet-encrypted at rest and never returned to the browser; leave the field blank when editing to keep the stored value.</p>
  </Callout>
  if(method==='service_principal_cert')return <Callout tone="info" title="Service principal with a certificate">
    <p>Same identity model as a client secret, but authentication uses a key pair — useful where secrets are not permitted. Create it with a self-signed certificate:</p>
    <CmdBlock cmd={'az ad sp create-for-rbac --name azureops-scheduler --create-cert --role "Virtual Machine Contributor" --scopes /subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>'}/>
    <p className="mt-2">Paste the full PEM bundle — private key first, then the certificate — into the field below. Rotating the certificate does not require a new app registration.</p>
    <pre className="mt-1.5 overflow-x-auto rounded border border-slate-200 bg-white px-2 py-1 font-mono text-[11px] leading-5 text-slate-700">{'-----BEGIN PRIVATE KEY-----\n…\n-----END PRIVATE KEY-----\n-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----'}</pre>
  </Callout>
  if(method==='az_cli_token')return <Callout tone="warn" title="Pasted Azure CLI token — short-lived">
    <ol className="list-decimal space-y-1 pl-4">
      <li>On your own computer, sign in to the tenant:<CmdBlock cmd={`az login --tenant ${tenant}`}/></li>
      <li>Get an ARM access token (valid about one hour):<CmdBlock cmd="az account get-access-token --resource https://management.azure.com --output json"/></li>
      <li>Paste the entire JSON output below. The token and its expiry are extracted automatically.</li>
    </ol>
    <p className="mt-2">The Azure CLI does not expose refresh tokens, so this credential cannot renew itself and <strong>scheduled starts will fail once it expires</strong>. Use it only for a quick connectivity test — choose <em>Azure CLI session</em> or a service principal for anything unattended.</p>
  </Callout>
  return null
}

function Toggle({label,checked,onChange}:{label:string;checked:boolean;onChange:(value:boolean)=>void}){return <label className="flex items-center gap-3"><input className="!w-auto" type="checkbox" checked={checked} onChange={e=>onChange(e.target.checked)}/><span>{label}</span></label>}
function Info({label,value}:{label:string;value:string}){return <div className="rounded-lg border border-slate-200 bg-slate-50 p-3"><p className="text-slate-500">{label}</p><p className="mt-1 text-slate-700">{value}</p></div>}
