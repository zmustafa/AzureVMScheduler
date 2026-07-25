import { useState, type FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Navigate, useNavigate } from 'react-router'
import { Activity, LockKeyhole } from 'lucide-react'
import { useAuth } from '../auth'
import { ErrorNotice } from '../components/Ui'
import { api } from '../api'
import type { SignInProvider } from '../types'

type AuthConfig = { local_login_enabled: boolean; providers: SignInProvider[] }

export function LoginPage() {
  const { user, login } = useAuth(); const navigate = useNavigate()
  const config = useQuery({queryKey:['auth-config'],queryFn:()=>api<AuthConfig>('/auth/config')})
  const [username, setUsername] = useState('admin'); const [password, setPassword] = useState(''); const [error, setError] = useState<unknown>(); const [busy, setBusy] = useState(false)
  if (user) return <Navigate to="/" replace />
  const submit = async (event: FormEvent) => { event.preventDefault(); setBusy(true); setError(undefined); try { await login(username, password); navigate('/') } catch (reason) { setError(reason) } finally { setBusy(false) } }
  return <main className="grid min-h-screen place-items-center bg-[radial-gradient(circle_at_top,#dbeafe_0,transparent_48%)] p-4"><section className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-8 shadow-xl shadow-slate-200/70">
    <div className="mb-8 flex items-center gap-4"><span className="grid h-12 w-12 place-items-center rounded-xl bg-azure text-white shadow-md shadow-blue-200"><Activity/></span><div><h1 className="text-2xl font-bold text-slate-900">Welcome to Azure VM Scheduler</h1><p className="muted">Sign in to manage VM start and stop schedules.</p></div></div>
    {(config.data?.providers ?? []).length > 0 && <div className="mb-5 space-y-2">
      {(config.data?.providers ?? []).map((provider) => <a key={provider.id} className="btn-primary w-full justify-center" href={`${provider.start_url}?return_url=${encodeURIComponent(window.location.origin)}`}>{provider.button_label}</a>)}
      <p className="pt-2 text-center text-xs uppercase tracking-wider text-slate-400">or</p>
    </div>}
    <form className="space-y-4" autoComplete="off" onSubmit={submit}>{Boolean(error) && <ErrorNotice error={error}/>}<div className="field"><label>Username</label><input autoComplete="off" value={username} onChange={e => setUsername(e.target.value)} required autoFocus /></div><div className="field"><label>Password</label><input type="password" autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)} required /></div><button className="btn-primary w-full" disabled={busy}><LockKeyhole size={17}/>{busy ? 'Signing in…' : config.data?.local_login_enabled===false?'Break-glass sign in':'Sign in locally'}</button></form>
    {config.data&&!config.data.local_login_enabled&&<p className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">Normal local login is disabled. Only the designated break-glass administrator can use this form.</p>}
    <p className="mt-6 text-center text-xs text-slate-500">Local-first · Credentials stay on this host</p>
  </section></main>
}