import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { RefreshCcw, Send, Ticket } from 'lucide-react'
import { api, json } from '../api'
import { useCan } from '../auth'
import { connectorIcon, deliveryTone, eventLabel, severityMeta, useConnectorCatalog } from '../lib/notify'
import { useDisplayTimezone } from '../lib/time'
import { useSort } from '../lib/sorting'
import { CopyBtn } from '../components/Help'
import { SortHeader } from '../components/SortHeader'
import { Chip, EmptyState, ErrorNotice, Field, PageHeader, Pagination, TableSkeleton } from '../components/Ui'
import type { NotificationDelivery, NotificationFeed, Paged } from '../types'

const PAGE_SIZE = 25
const STATUSES = ['pending', 'sent', 'failed', 'skipped']

/** Incident numbers are the thing operators actually search for, so make them prominent and copyable. */
export function ExternalRef({ value, servicenow }: { value: string; servicenow: boolean }) {
  if (!value) return <span className="text-slate-400">—</span>
  if (!servicenow) return <span className="font-mono text-xs text-slate-600">{value}</span>
  return <span className="inline-flex items-center gap-1.5">
    <span className="inline-flex items-center gap-1 rounded-md border border-violet-200 bg-violet-50 px-2 py-0.5 font-mono text-xs font-semibold text-violet-900"><Ticket size={12} aria-hidden="true" />{value}</span>
    <CopyBtn value={value} label="Copy" />
  </span>
}

/** Delivery log for outbound notifications, with a guarded retry for failures. */
export function DeliveriesPage() {
  const canManage = useCan('notifications.manage')
  const client = useQueryClient()
  const { format } = useDisplayTimezone()
  const [status, setStatus] = useState('')
  const [connectorId, setConnectorId] = useState('')
  const [offset, setOffset] = useState(0)
  const { sort, toggle } = useSort({ key: 'created_at', direction: 'desc' }, ['created_at'])

  const catalog = useConnectorCatalog()
  const connectorById = useMemo(() => new Map((catalog.data?.connectors ?? []).map((item) => [item.id, item])), [catalog.data])

  const search = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) })
  if (status) search.set('status', status)
  if (connectorId) search.set('connector_id', connectorId)
  search.set('sort', sort.key)
  search.set('direction', sort.direction)

  const query = useQuery({
    queryKey: ['deliveries', status, connectorId, offset, sort.key, sort.direction],
    queryFn: () => api<Paged<NotificationDelivery>>(`/deliveries?${search.toString()}`),
    // Pending rows are still being attempted in the background, so keep the table live until they settle.
    refetchInterval: (item) => ((item.state.data?.items ?? []).some((row) => row.status === 'pending') ? 5_000 : false),
  })

  // Deliveries carry only the event id, so pull a recent page of events to label each row.
  const events = useQuery({
    queryKey: ['notifications', 'feed', 'for-deliveries'],
    queryFn: () => api<NotificationFeed>('/notifications?limit=200'),
    staleTime: 30_000,
  })
  const eventById = useMemo(() => new Map((events.data?.items ?? []).map((item) => [item.id, item])), [events.data])

  const retry = useMutation({
    mutationFn: (id: string) => api<NotificationDelivery>(`/deliveries/${id}/retry`, json('POST')),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['deliveries'] }),
  })

  const rows = query.data?.items ?? []
  const changeFilter = (apply: () => void) => { apply(); setOffset(0) }

  const header = <PageHeader title="Delivery history" description="Every outbound notification attempt, its connector, and the reference it produced." />

  return <>
    {header}

    <div className="card mb-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <Field label="Status">
        <select value={status} onChange={(event) => changeFilter(() => setStatus(event.target.value))}>
          <option value="">Any status</option>
          {STATUSES.map((value) => <option key={value} value={value}>{value.replaceAll('_', ' ')}</option>)}
        </select>
      </Field>
      <Field label="Connector">
        <select value={connectorId} onChange={(event) => changeFilter(() => setConnectorId(event.target.value))}>
          <option value="">Any connector</option>
          {(catalog.data?.connectors ?? []).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </Field>
      <div className="field sm:col-span-2 lg:col-span-2 lg:self-end">
        <p className="text-xs text-slate-500">Rules decide what is sent here. <Link className="link" to="/settings/notifications">Edit notification rules</Link> or <Link className="link" to="/settings/connectors">manage connectors</Link>.</p>
      </div>
    </div>

    {retry.error && <div className="mb-4"><ErrorNotice error={retry.error} /></div>}

    {query.isLoading ? <TableSkeleton columns={6} />
      : query.error ? <ErrorNotice error={query.error} />
        : rows.length === 0 ? <EmptyState
          icon={<Send size={22} />}
          title={status || connectorId ? 'No deliveries match these filters' : 'Nothing has been delivered yet'}
          description={status || connectorId ? 'Clear the filters to see the full history.' : 'Outbound attempts appear here once a rule matches an event and a connector is selected.'}
        /> : <div className="surface">
          {/* Desktop: a dense table. Mobile: the same rows stacked as cards. */}
          <div className="hidden overflow-x-auto lg:block">
            <table className="u-table">
              <thead><tr>
                <SortHeader label="Time" sortKey="created_at" sort={sort} onSort={toggle} />
                <SortHeader label="Event" />
                <SortHeader label="Connector" sortKey="connector_label" sort={sort} onSort={toggle} />
                <SortHeader label="Status" sortKey="status" sort={sort} onSort={toggle} />
                <SortHeader label="Reference" />
                <SortHeader label="Detail" />
                <SortHeader label={<span className="sr-only">Actions</span>} />
              </tr></thead>
              <tbody>{rows.map((row) => {
                const event = eventById.get(row.event_id)
                const connector = connectorById.get(row.connector_id)
                const Icon = connectorIcon(connector?.type ?? '')
                const severity = event ? severityMeta(event.severity) : null
                return <tr key={row.id}>
                  <td className="whitespace-nowrap text-xs text-slate-600">{format(row.created_at)}{row.sent_at && <span className="block text-slate-500">sent {format(row.sent_at)}</span>}</td>
                  <td className="text-sm">
                    <span className="block font-medium text-slate-900">{event ? eventLabel(event.type) : 'Event no longer listed'}</span>
                    {severity && <Chip tone={severity.tone}>{severity.label}</Chip>}
                  </td>
                  <td className="text-sm text-slate-700"><span className="inline-flex items-center gap-1.5"><Icon size={14} className="shrink-0 text-slate-500" />{row.connector_label || connector?.name || row.connector_id}</span></td>
                  <td><Chip tone={deliveryTone(row.status)}>{row.status}</Chip><span className="ml-2 text-xs text-slate-500">{row.attempts} attempt{row.attempts === 1 ? '' : 's'}</span></td>
                  <td><ExternalRef value={row.external_ref} servicenow={connector?.type === 'servicenow'} /></td>
                  <td className="max-w-[22rem] text-xs text-slate-700">{row.detail || '—'}</td>
                  <td className="text-right">{canManage && row.status === 'failed' && <button type="button" className="btn-secondary !py-1" disabled={retry.isPending} onClick={() => retry.mutate(row.id)}><RefreshCcw size={14} />Retry</button>}</td>
                </tr>
              })}</tbody>
            </table>
          </div>
          <ul className="divide-y divide-slate-200 lg:hidden">{rows.map((row) => {
            const event = eventById.get(row.event_id)
            const connector = connectorById.get(row.connector_id)
            const Icon = connectorIcon(connector?.type ?? '')
            const severity = event ? severityMeta(event.severity) : null
            return <li key={row.id} className="space-y-2 p-4">
              <div className="flex flex-wrap items-center gap-2">
                <Chip tone={deliveryTone(row.status)}>{row.status}</Chip>
                {severity && <Chip tone={severity.tone}>{severity.label}</Chip>}
                <span className="ml-auto text-xs text-slate-500">{format(row.created_at)}</span>
              </div>
              <p className="font-medium text-slate-900">{event ? eventLabel(event.type) : 'Event no longer listed'}</p>
              <p className="flex items-center gap-1.5 text-sm text-slate-700"><Icon size={14} className="shrink-0 text-slate-500" />{row.connector_label || connector?.name || row.connector_id} · {row.attempts} attempt{row.attempts === 1 ? '' : 's'}</p>
              <ExternalRef value={row.external_ref} servicenow={connector?.type === 'servicenow'} />
              {row.detail && <p className="text-xs text-slate-600">{row.detail}</p>}
              {canManage && row.status === 'failed' && <button type="button" className="btn-secondary !py-1" disabled={retry.isPending} onClick={() => retry.mutate(row.id)}><RefreshCcw size={14} />Retry</button>}
            </li>
          })}</ul>
          <Pagination total={query.data?.total ?? 0} limit={PAGE_SIZE} offset={offset} onChange={setOffset} />
        </div>}
  </>
}
