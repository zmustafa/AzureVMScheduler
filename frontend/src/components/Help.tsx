import { useState, type ReactNode } from 'react'
import { BookOpen, Check, ChevronDown, ChevronRight, Copy } from 'lucide-react'

const TONES = {
  info: 'border-blue-200 bg-blue-50 text-blue-900',
  success: 'border-emerald-200 bg-emerald-50 text-emerald-900',
  warn: 'border-amber-200 bg-amber-50 text-amber-900',
  neutral: 'border-slate-200 bg-slate-50 text-slate-700',
} as const

export function CopyBtn({ value, label = 'Copy' }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false)
  return <button type="button" title="Copy to clipboard" onClick={() => { void navigator.clipboard?.writeText(value); setCopied(true); setTimeout(() => setCopied(false), 1400) }} className="inline-flex shrink-0 items-center gap-1 rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] font-medium text-slate-600 transition hover:border-slate-400 hover:text-slate-900">{copied ? <Check size={12} className="text-emerald-600"/> : <Copy size={12}/>}{copied ? 'Copied' : label}</button>
}

export function CmdBlock({ cmd }: { cmd: string }) {
  return <div className="mt-1.5 flex items-stretch gap-1.5">
    <CopyBtn value={cmd}/>
    <pre className="flex-1 overflow-x-auto rounded border border-slate-200 bg-white px-2 py-1 font-mono text-[11px] leading-5 text-slate-800">{cmd}</pre>
  </div>
}

export function CopyRow({ label, value }: { label: string; value: string }) {
  return <div className="mt-1.5 flex items-center gap-2 rounded border border-slate-200 bg-white px-2 py-1">
    <span className="shrink-0 text-[11px] font-medium uppercase tracking-wide text-slate-400">{label}</span>
    <code className="flex-1 overflow-x-auto whitespace-nowrap font-mono text-[11px] text-slate-800">{value}</code>
    <CopyBtn value={value}/>
  </div>
}

export function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return <li className="flex gap-3">
    <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-blue-600 text-[11px] font-semibold text-white">{n}</span>
    <div className="min-w-0 flex-1"><p className="text-sm font-semibold text-slate-800">{title}</p><div className="mt-1 text-xs leading-relaxed text-slate-600">{children}</div></div>
  </li>
}

export function Callout({ tone = 'info', title, children }: { tone?: keyof typeof TONES; title?: string; children: ReactNode }) {
  return <div className={`rounded-lg border p-3 text-xs leading-relaxed ${TONES[tone]}`}>{title && <p className="mb-1.5 text-sm font-semibold">{title}</p>}{children}</div>
}

export function SetupGuide({ title, subtitle, defaultOpen = false, children }: { title: string; subtitle?: string; defaultOpen?: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(defaultOpen)
  return <div className="rounded-lg border border-blue-200 bg-blue-50/50">
    <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-sm font-semibold text-blue-900 transition hover:bg-blue-100/60">
      {open ? <ChevronDown size={16}/> : <ChevronRight size={16}/>}<BookOpen size={16}/>
      <span className="flex-1">{title}</span>
      {subtitle && <span className="hidden text-xs font-normal text-blue-700 sm:inline">{subtitle}</span>}
    </button>
    {open && <div className="border-t border-blue-200 bg-white/70 p-4">{children}</div>}
  </div>
}

export function PermissionTable({ rows, columns = ['Permission', 'Type', 'Why it is needed'] }: { rows: readonly { name: string; type: string; desc: string }[]; columns?: readonly [string, string, string] | string[] }) {
  return <div className="mt-2 overflow-x-auto rounded-lg border border-slate-200">
    <table className="w-full text-left text-xs">
      <thead className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-500"><tr>{columns.map((column) => <th key={column} className="px-3 py-2 font-semibold">{column}</th>)}</tr></thead>
      <tbody className="divide-y divide-slate-200">{rows.map(row => <tr key={row.name}><td className="whitespace-nowrap px-3 py-2 font-mono text-slate-800">{row.name}</td><td className="whitespace-nowrap px-3 py-2 text-slate-500">{row.type}</td><td className="px-3 py-2 text-slate-600">{row.desc}</td></tr>)}</tbody>
    </table>
  </div>
}
