import { useState } from 'react'
import { Link } from 'react-router'
import { CheckCircle2, ChevronDown, ChevronRight, CircleAlert, TriangleAlert } from 'lucide-react'
import type { ReadinessCheck } from '../types'

const TONE = {
  error: { row: 'border-rose-200 bg-rose-50', text: 'text-rose-900', icon: CircleAlert, iconClass: 'text-rose-600' },
  warning: { row: 'border-amber-200 bg-amber-50', text: 'text-amber-900', icon: TriangleAlert, iconClass: 'text-amber-600' },
  info: { row: 'border-slate-200 bg-white', text: 'text-slate-800', icon: CircleAlert, iconClass: 'text-slate-500' },
} as const

/**
 * Pre-flight problems that would make the next wave fail or silently do nothing.
 * Collapses to a single green line when the estate is ready, so a healthy dashboard stays quiet.
 */
export function ReadinessStrip({ checks, loading }: { checks: ReadinessCheck[]; loading?: boolean }) {
  const [open, setOpen] = useState(true)
  const errors = checks.filter((item) => item.severity === 'error').length
  const warnings = checks.filter((item) => item.severity === 'warning').length

  if (loading) return <div className="card mb-4"><div className="skeleton h-5 w-64" /></div>

  if (checks.length === 0) {
    return <div className="mb-4 flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm text-emerald-900">
      <CheckCircle2 size={16} className="shrink-0 text-emerald-600" aria-hidden="true" />
      <span><strong className="font-semibold">Ready.</strong> Credentials are valid, every enabled schedule resolves to virtual machines, and nothing is stuck.</span>
    </div>
  }

  const summary = [errors && `${errors} blocking`, warnings && `${warnings} to review`].filter(Boolean).join(' · ')

  return <section className="mb-4 overflow-hidden rounded-lg border border-slate-200 bg-white" aria-label="Readiness checks">
    <button
      type="button"
      onClick={() => setOpen(!open)}
      aria-expanded={open}
      className={`flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm font-semibold ${errors ? 'bg-rose-50 text-rose-900' : 'bg-amber-50 text-amber-900'}`}
    >
      {open ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronRight size={16} aria-hidden="true" />}
      {errors ? <CircleAlert size={16} className="text-rose-600" aria-hidden="true" /> : <TriangleAlert size={16} className="text-amber-600" aria-hidden="true" />}
      Before the next wave — {summary}
    </button>

    {open && <ul className="divide-y divide-slate-100">
      {checks.map((check) => {
        const tone = TONE[check.severity]
        const Icon = tone.icon
        return <li key={check.id} className={`flex flex-wrap items-start gap-3 border-l-4 px-4 py-3 ${tone.row}`}>
          <Icon size={16} className={`mt-0.5 shrink-0 ${tone.iconClass}`} aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <p className={`text-sm font-semibold ${tone.text}`}>{check.title}</p>
            <p className="text-xs text-slate-600">{check.detail}</p>
          </div>
          <Link className="btn-secondary !py-1 text-xs" to={check.link}>Fix</Link>
        </li>
      })}
    </ul>}
  </section>
}
