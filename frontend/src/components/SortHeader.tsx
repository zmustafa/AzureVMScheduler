import { ChevronDown, ChevronUp, ChevronsUpDown } from 'lucide-react'
import type { ReactNode } from 'react'
import type { Sort } from '../lib/sorting'

/**
 * A sortable column header. Columns without a `sortKey` render as plain headers so the
 * grid never offers a sort it cannot actually perform.
 */
export function SortHeader({ label, sortKey, sort, onSort, align = 'left', className = '' }: {
  label: ReactNode
  sortKey?: string
  sort?: Sort
  onSort?: (key: string) => void
  align?: 'left' | 'right'
  className?: string
}) {
  const active = !!sortKey && sort?.key === sortKey
  const ariaSort = active ? (sort?.direction === 'asc' ? 'ascending' : 'descending') : 'none'

  if (!sortKey || !onSort) return <th scope="col" className={className}>{label}</th>

  return <th scope="col" aria-sort={ariaSort} className={className}>
    <button
      type="button"
      onClick={() => onSort(sortKey)}
      className={`group flex w-full items-center gap-1 text-left font-semibold uppercase tracking-wider transition hover:text-slate-900 ${align === 'right' ? 'justify-end' : ''} ${active ? 'text-slate-900' : ''}`}
      title={`Sort by ${typeof label === 'string' ? label : sortKey}`}
    >
      {label}
      {active
        ? (sort?.direction === 'asc' ? <ChevronUp size={13} aria-hidden="true" /> : <ChevronDown size={13} aria-hidden="true" />)
        : <ChevronsUpDown size={13} aria-hidden="true" className="opacity-0 transition group-hover:opacity-60" />}
    </button>
  </th>
}
