import { cloneElement, isValidElement, useEffect, useId, useRef, type ChangeEvent, type ReactElement, type ReactNode } from 'react'
import { Search } from 'lucide-react'
import { formatInZone } from '../lib/time'

export function PageHeader({ title, description, action }: { title: string; description: string; action?: ReactNode }) { return <div className="mb-6 flex flex-wrap items-end justify-between gap-4"><div><h1 className="page-title">{title}</h1><p className="mt-1 muted">{description}</p></div>{action}</div> }
export function Loading() { return <div className="card animate-pulse text-slate-500">Loading…</div> }
export function ErrorNotice({ error }: { error: unknown }) { return <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{error instanceof Error ? error.message : 'Something went wrong'}</div> }
export function Empty({ children }: { children: ReactNode }) { return <div className="card py-12 text-center text-slate-500">{children}</div> }

/** Every timestamp in the product is rendered with an explicit zone label. */
export function formatDate(value?: string | null, timeZone?: string) { return formatInZone(value, timeZone) }

export function Skeleton({ className = 'h-4 w-full' }: { className?: string }) { return <div className={`skeleton ${className}`} /> }

export function TableSkeleton({ rows = 6, columns = 5 }: { rows?: number; columns?: number }) {
  return <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm" aria-busy="true" aria-label="Loading rows">
    {Array.from({ length: rows }).map((_, row) => <div key={row} className="flex items-center gap-4 border-t border-slate-200 px-4 py-3 first:border-t-0">
      {Array.from({ length: columns }).map((__, column) => <Skeleton key={column} className={`h-4 ${column === 0 ? 'w-1/3' : 'flex-1'}`} />)}
    </div>)}
  </div>
}

export function EmptyState({ icon, title, description, action }: { icon?: ReactNode; title: string; description: string; action?: ReactNode }) {
  return <div className="card flex flex-col items-center gap-3 py-14 text-center">
    {icon && <span className="grid h-12 w-12 place-items-center rounded-full bg-blue-50 text-blue-700">{icon}</span>}
    <div><p className="text-base font-semibold text-slate-900">{title}</p><p className="mx-auto mt-1 max-w-md muted">{description}</p></div>
    {action}
  </div>
}

/** Search box that is focused by the global "/" shortcut. */
export function SearchInput({ value, onChange, placeholder = 'Search', label }: { value: string; onChange: (value: string) => void; placeholder?: string; label?: string }) {
  const input = useRef<HTMLInputElement>(null)
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const typing = !!target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
      if (event.key === '/' && !typing) { event.preventDefault(); input.current?.focus() }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])
  return <div className="relative w-full max-w-md">
    <Search className="pointer-events-none absolute left-3 top-2.5 text-slate-500" size={18} aria-hidden="true" />
    <input ref={input} className="!pl-10" autoComplete="off" placeholder={`${placeholder}  ( / )`} aria-label={label ?? placeholder} value={value} onChange={(event: ChangeEvent<HTMLInputElement>) => onChange(event.target.value)} />
  </div>
}

export function Field({ label, hint, wide, children }: { label: string; hint?: string; wide?: boolean; children: ReactNode }) {
  const generated = useId()
  const hintId = hint ? `${generated}-hint` : undefined
  // Bind the label (and hint) to the control so screen readers announce them instead of falling back to the placeholder.
  let control = children
  let controlId: string | undefined
  if (isValidElement(children)) {
    const element = children as ReactElement<{ id?: string; 'aria-describedby'?: string; autoComplete?: string; type?: string }>
    controlId = element.props.id ?? generated
    // Browsers happily autofill saved credentials into unrelated configuration fields — a saved
    // password landing in "Client secret", or a username in "Tenant ID". Opt every control out.
    // "new-password" is the only value Chrome honours for password inputs.
    const isFormControl = element.type === 'input' || element.type === 'textarea' || element.type === 'select'
    const autoComplete = element.props.autoComplete ?? (element.props.type === 'password' ? 'new-password' : 'off')
    control = cloneElement(element, {
      id: controlId,
      'aria-describedby': [element.props['aria-describedby'], hintId].filter(Boolean).join(' ') || undefined,
      ...(isFormControl ? { autoComplete } : {}),
    })
  }
  return <div className={`field ${wide ? 'md:col-span-2' : ''}`}>
    <label htmlFor={controlId}>{label}</label>
    {control}
    {hint && <p id={hintId} className="text-xs text-slate-500">{hint}</p>}
  </div>
}

/** Accessible on/off switch used for optimistic enable/disable toggles. */
export function Toggle({ checked, onChange, label, disabled, busy }: { checked: boolean; onChange: (next: boolean) => void; label: string; disabled?: boolean; busy?: boolean }) {
  return <button
    type="button"
    role="switch"
    aria-checked={checked}
    aria-label={label}
    disabled={disabled || busy}
    onClick={() => onChange(!checked)}
    className={`inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure focus-visible:ring-offset-2 disabled:opacity-50 ${checked ? 'border-emerald-300 bg-emerald-500' : 'border-slate-300 bg-slate-200'}`}
  >
    <span className={`ml-0.5 h-5 w-5 rounded-full bg-white shadow transition ${checked ? 'translate-x-5' : 'translate-x-0'}`} />
  </button>
}

const CHIP_TONES = {
  neutral: 'border-slate-200 bg-slate-100 text-slate-700',
  info: 'border-sky-200 bg-sky-50 text-sky-800',
  success: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  warn: 'border-amber-200 bg-amber-50 text-amber-900',
  danger: 'border-rose-200 bg-rose-50 text-rose-800',
  accent: 'border-violet-200 bg-violet-50 text-violet-800',
} as const

/** Status chips always carry text (and usually an icon) so colour is never the only signal. */
export function Chip({ tone = 'neutral', icon, children, title }: { tone?: keyof typeof CHIP_TONES; icon?: ReactNode; children: ReactNode; title?: string }) {
  return <span title={title} className={`chip ${CHIP_TONES[tone]}`}>{icon}{children}</span>
}

export function Pagination({ total, limit, offset, onChange }: { total: number; limit: number; offset: number; onChange: (offset: number) => void }) {
  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + limit, total)
  return <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-white px-4 py-3 text-sm text-slate-600">
    <span>Showing {from}–{to} of {total}</span>
    <div className="flex gap-2">
      <button type="button" className="btn-secondary !py-1" disabled={offset === 0} onClick={() => onChange(Math.max(offset - limit, 0))}>Previous</button>
      <button type="button" className="btn-secondary !py-1" disabled={to >= total} onClick={() => onChange(offset + limit)}>Next</button>
    </div>
  </div>
}
