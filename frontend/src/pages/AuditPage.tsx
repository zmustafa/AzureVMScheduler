import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Search } from 'lucide-react'
import { api } from '../api'
import { useClientSort } from '../lib/sorting'
import { SortHeader } from '../components/SortHeader'
import { Empty, ErrorNotice, Loading, PageHeader, formatDate } from '../components/Ui'
import type { AuditLog } from '../types'

/** The whole page is fetched at once, so this grid sorts in the browser. */
const SORTERS: Record<string, (row: AuditLog) => unknown> = {
  created_at: (row) => row.created_at,
  action: (row) => row.action,
  target: (row) => `${row.target_type}:${row.target_id ?? ''}`,
  detail: (row) => row.detail,
}

export function AuditPage() {
  const [action, setAction] = useState('')
  const query = useQuery({
    queryKey: ['audit', action],
    queryFn: () => api<AuditLog[]>(`/audit?limit=200${action ? `&action=${encodeURIComponent(action)}` : ''}`),
  })
  const { rows, sort, toggle } = useClientSort(query.data ?? [], SORTERS, { key: 'created_at', direction: 'desc' }, ['created_at'])

  return <>
    <PageHeader title="Audit log" description="Security-sensitive actions and scheduler outcomes, newest first." />

    <div className="relative mb-4 max-w-md">
      <Search className="absolute left-3 top-2.5 text-slate-500" size={18} />
      <input className="!pl-10" autoComplete="off" placeholder="Filter by action" aria-label="Filter by action" value={action} onChange={(event) => setAction(event.target.value)} />
    </div>

    {query.isLoading ? <Loading />
      : query.error ? <ErrorNotice error={query.error} />
        : rows.length === 0 ? <Empty>No audit records found.</Empty>
          : <div className="surface max-h-[70vh] overflow-auto">
            <table className="u-table">
              <thead><tr>
                <SortHeader label="Time" sortKey="created_at" sort={sort} onSort={toggle} />
                <SortHeader label="Action" sortKey="action" sort={sort} onSort={toggle} />
                <SortHeader label="Target" sortKey="target" sort={sort} onSort={toggle} />
                <SortHeader label="Detail" sortKey="detail" sort={sort} onSort={toggle} />
              </tr></thead>
              <tbody>{rows.map((item) => <tr key={item.id}>
                <td className="whitespace-nowrap text-xs text-slate-600">{formatDate(item.created_at)}</td>
                <td className="text-sm font-medium text-blue-700">{item.action}</td>
                <td className="truncate text-xs text-slate-600">{item.target_type}:{item.target_id?.slice(0, 8) ?? '—'}</td>
                <td className="max-w-[28rem] truncate text-xs text-slate-500"><code>{item.detail}</code></td>
              </tr>)}</tbody>
            </table>
          </div>}
  </>
}
