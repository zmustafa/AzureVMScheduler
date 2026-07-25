import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, Globe2 } from 'lucide-react'
import { zoneLabel } from '../lib/time'

const COMMON = ['America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'America/Sao_Paulo', 'Europe/London', 'Europe/Dublin', 'Europe/Paris', 'Europe/Berlin', 'Europe/Madrid', 'Asia/Dubai', 'Asia/Kolkata', 'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney', 'UTC']

function allZones(): string[] {
  const supported = (Intl as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf
  try {
    const values = supported?.('timeZone') ?? []
    return values.length ? values : COMMON
  } catch {
    return COMMON
  }
}

/** Searchable IANA timezone picker with a short list of common zones pinned to the top. */
export function TimezonePicker({ value, onChange, id, disabled }: { value: string; onChange: (zone: string) => void; id?: string; disabled?: boolean }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const container = useRef<HTMLDivElement>(null)
  const zones = useMemo(allZones, [])

  useEffect(() => {
    if (!open) return
    const onClick = (event: MouseEvent) => { if (!container.current?.contains(event.target as Node)) setOpen(false) }
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onClick); document.removeEventListener('keydown', onKey) }
  }, [open])

  const needle = query.trim().toLowerCase()
  const matches = useMemo(() => zones.filter((zone) => zone.toLowerCase().includes(needle)).slice(0, 200), [zones, needle])
  const common = COMMON.filter((zone) => zones.includes(zone) && zone.toLowerCase().includes(needle))

  const option = (zone: string) => <li key={zone} role="option" aria-selected={zone === value}>
    <button type="button" onClick={() => { onChange(zone); setOpen(false); setQuery('') }} className={`flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-sm transition hover:bg-blue-50 ${zone === value ? 'font-semibold text-blue-800' : 'text-slate-700'}`}>
      <span className="truncate">{zone}</span>
      <span className="flex shrink-0 items-center gap-1 text-xs text-slate-500">{zoneLabel(zone)}{zone === value && <Check size={13} className="text-blue-700" />}</span>
    </button>
  </li>

  return <div className="relative" ref={container}>
    <button type="button" id={id} disabled={disabled} aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(!open)} className="flex w-full items-center justify-between gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure disabled:bg-slate-100">
      <span className="flex min-w-0 items-center gap-2"><Globe2 size={15} className="shrink-0 text-slate-500" /><span className="truncate">{value || 'Select timezone'}</span></span>
      <span className="flex shrink-0 items-center gap-1 text-xs text-slate-500">{value ? zoneLabel(value) : ''}<ChevronDown size={15} /></span>
    </button>
    {open && <div className="absolute z-40 mt-1 w-full rounded-lg border border-slate-200 bg-white shadow-xl">
      <div className="border-b border-slate-200 p-2"><input autoFocus autoComplete="off" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search timezones" aria-label="Search timezones" /></div>
      <ul role="listbox" aria-label="Timezones" className="max-h-72 overflow-y-auto py-1">
        {common.length > 0 && <>
          <li className="px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400" aria-hidden="true">Common</li>
          {common.map(option)}
          <li className="my-1 border-t border-slate-100" aria-hidden="true" />
        </>}
        <li className="px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400" aria-hidden="true">All timezones</li>
        {matches.length ? matches.map(option) : <li className="px-3 py-3 text-sm text-slate-500">No timezone matches “{query}”.</li>}
      </ul>
    </div>}
  </div>
}
