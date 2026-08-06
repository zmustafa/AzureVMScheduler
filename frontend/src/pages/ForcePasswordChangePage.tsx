import { useState, type FormEvent } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { KeyRound, LockKeyhole } from 'lucide-react'
import { api, json } from '../api'
import { useAuth } from '../auth'
import { ErrorNotice, Loading } from '../components/Ui'
import type { User } from '../types'

type PasswordPolicy = { min_length: number; require_upper: boolean; require_lower: boolean; require_number: boolean; require_symbol: boolean }

/**
 * Standalone screen shown instead of the application while `must_change_password` is set.
 *
 * The server refuses every path outside a small allowlist until the password is changed, so this
 * screen must only call endpoints on that allowlist (`/auth/me`, `/auth/change-password`,
 * `/auth/logout`). Rendering the normal shell here would leave the user staring at 403s with no
 * way to satisfy the requirement.
 */
export function ForcePasswordChangePage() {
  const { user, logout, refresh } = useAuth()
  const [message, setMessage] = useState('')
  const me = useQuery({ queryKey: ['auth-me-policy'], queryFn: () => api<{ user: User; password_policy: PasswordPolicy }>('/auth/me') })
  const change = useMutation({
    mutationFn: (body: unknown) => api('/auth/change-password', json('POST', body)),
    onSuccess: () => { setMessage(''); void refresh() },
  })
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setMessage('')
    const form = new FormData(event.currentTarget)
    if (form.get('new_password') !== form.get('confirm')) { setMessage('New passwords do not match.'); return }
    change.mutate({ current_password: form.get('current_password'), new_password: form.get('new_password') })
  }
  const policy = me.data?.password_policy
  const rules = policy ? [
    `At least ${policy.min_length} characters`,
    ...(policy.require_upper ? ['An uppercase letter'] : []),
    ...(policy.require_lower ? ['A lowercase letter'] : []),
    ...(policy.require_number ? ['A number'] : []),
    ...(policy.require_symbol ? ['A symbol'] : []),
  ] : []
  return <main className="grid min-h-screen place-items-center bg-[radial-gradient(circle_at_top,#dbeafe_0,transparent_48%)] p-4">
    <section className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-8 shadow-xl shadow-slate-200/70">
      <div className="mb-6 flex items-center gap-4">
        <span className="grid h-12 w-12 place-items-center rounded-xl bg-azure text-white shadow-md shadow-blue-200"><KeyRound/></span>
        <div><h1 className="text-2xl font-bold text-slate-900">Choose a new password</h1><p className="muted">Signed in as {user?.username}.</p></div>
      </div>
      <p className="mb-5 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">This account still uses its initial password. Set a new one to continue — the rest of the application stays locked until you do.</p>
      {me.isLoading ? <Loading/> : <>
        {me.error ? <ErrorNotice error={me.error}/> : null}
        <form className="space-y-4" autoComplete="off" onSubmit={submit}>
          {Boolean(change.error) && <ErrorNotice error={change.error}/>}
          {message && <p className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{message}</p>}
          <div className="field"><label>Current password</label><input name="current_password" type="password" autoComplete="new-password" required autoFocus/></div>
          <div className="field"><label>New password</label><input name="new_password" type="password" autoComplete="new-password" required/></div>
          <div className="field"><label>Confirm new password</label><input name="confirm" type="password" autoComplete="new-password" required/></div>
          {rules.length > 0 && <ul className="list-inside list-disc rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-600">{rules.map(rule => <li key={rule}>{rule}</li>)}</ul>}
          <button className="btn-primary w-full" disabled={change.isPending}><LockKeyhole size={17}/>{change.isPending ? 'Updating…' : 'Update password'}</button>
        </form>
      </>}
      <button className="mt-6 w-full text-center text-xs font-medium text-blue-700 hover:text-blue-800" onClick={() => void logout()}>Sign out instead</button>
    </section>
  </main>
}
