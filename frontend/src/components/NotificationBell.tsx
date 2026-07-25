import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { Bell, CheckCheck, Inbox } from 'lucide-react'
import { api, json } from '../api'
import { SeverityDot, UNREAD_KEY, eventLabel, relativeText, severityMeta } from '../lib/notify'
import { serverNow, useTick } from '../lib/time'
import { Skeleton } from './Ui'
import type { NotificationEvent, NotificationFeed } from '../types'

const FEED_KEY = ['notifications', 'feed'] as const

/** Deep link into whatever the event is about, most specific target first. */
function targetFor(event: NotificationEvent): string | null {
  if (event.run_id) return `/runs/${event.run_id}`
  if (event.schedule_id) return `/schedules/${event.schedule_id}`
  if (event.group_id) return `/applications/${event.group_id}`
  return null
}

function Row({ event, onRead, onNavigate, busy }: { event: NotificationEvent; onRead: (id: string) => void; onNavigate: () => void; busy: boolean }) {
  const target = targetFor(event)
  const meta = severityMeta(event.severity)
  const title = <span className="flex items-start gap-2">
    <span className="mt-1"><SeverityDot severity={event.severity} /></span>
    <span className="min-w-0 flex-1">
      <span className="block truncate text-sm font-medium text-slate-900">{event.title || eventLabel(event.type)}</span>
      <span className="mt-0.5 block text-xs text-slate-500">{meta.label} · {eventLabel(event.type)} · {relativeText(event.created_at, serverNow())}</span>
    </span>
  </span>
  return <li className={`px-3 py-2.5 ${event.read ? '' : 'bg-blue-50/50'}`}>
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        {target
          ? <Link to={target} onClick={onNavigate} className="block rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure">{title}</Link>
          : <div>{title}</div>}
        {event.body && <p className="mt-1 line-clamp-2 pl-4 text-xs text-slate-600">{event.body}</p>}
      </div>
      {!event.read && <button type="button" className="shrink-0 rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] font-medium text-slate-600 transition hover:border-slate-400 hover:text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure disabled:opacity-50" disabled={busy} onClick={() => onRead(event.id)}>Mark read</button>}
    </div>
  </li>
}

/**
 * Header bell. The unread count polls only while the tab is visible and refreshes
 * immediately on `visibilitychange`; the feed itself is only fetched when opened.
 */
export function NotificationBell({ unread }: { unread: number }) {
  const client = useQueryClient()
  const [open, setOpen] = useState(false)
  const container = useRef<HTMLDivElement>(null)
  useTick(30_000) // keeps the "3m ago" labels honest while the panel is open

  useEffect(() => {
    if (!open) return
    const onClick = (event: MouseEvent) => { if (!container.current?.contains(event.target as Node)) setOpen(false) }
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onClick); document.removeEventListener('keydown', onKey) }
  }, [open])

  const feed = useQuery({
    queryKey: FEED_KEY,
    enabled: open,
    queryFn: () => api<NotificationFeed>('/notifications?limit=15'),
  })

  const invalidate = () => {
    void client.invalidateQueries({ queryKey: FEED_KEY })
    void client.invalidateQueries({ queryKey: UNREAD_KEY })
  }
  const markOne = useMutation({ mutationFn: (id: string) => api(`/notifications/${id}/read`, json('POST')), onSuccess: invalidate })
  const markAll = useMutation({ mutationFn: () => api('/notifications/read-all', json('POST')), onSuccess: invalidate })

  const items = feed.data?.items ?? []
  const badge = unread > 99 ? '99+' : String(unread)

  return <div className="relative" ref={container}>
    <button
      type="button"
      className="btn-secondary relative !px-2 !py-1"
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-label={unread > 0 ? `Notifications, ${unread} unread` : 'Notifications'}
      onClick={() => setOpen(!open)}
    >
      <Bell size={16} />
      {unread > 0 && <span className="absolute -right-1.5 -top-1.5 grid min-w-[1.15rem] place-items-center rounded-full bg-rose-600 px-1 text-[10px] font-bold leading-4 text-white">{badge}</span>}
    </button>
    <span className="sr-only" aria-live="polite">{unread > 0 ? `${unread} unread notification${unread === 1 ? '' : 's'}` : 'No unread notifications'}</span>

    {open && <div role="dialog" aria-label="Notifications" className="absolute right-0 z-40 mt-2 w-[22rem] max-w-[calc(100vw-2rem)] rounded-xl border border-slate-200 bg-white shadow-2xl">
      <header className="flex items-center justify-between gap-2 border-b border-slate-200 px-3 py-2">
        <p className="text-sm font-semibold text-slate-900">Notifications</p>
        <button type="button" className="inline-flex items-center gap-1 text-xs font-medium text-blue-700 transition hover:text-blue-800 disabled:opacity-50" disabled={unread === 0 || markAll.isPending} onClick={() => markAll.mutate()}><CheckCheck size={13} />Mark all read</button>
      </header>

      {feed.isLoading ? <div className="space-y-2 p-3">{[0, 1, 2].map((key) => <Skeleton key={key} className="h-9 w-full" />)}</div>
        : feed.error ? <p className="p-4 text-sm text-rose-700">The notification feed could not be loaded.</p>
          : items.length === 0 ? <div className="flex flex-col items-center gap-2 px-4 py-8 text-center">
            <span className="grid h-10 w-10 place-items-center rounded-full bg-blue-50 text-blue-700"><Inbox size={18} /></span>
            <p className="text-sm font-medium text-slate-900">You are all caught up</p>
            <p className="text-xs text-slate-500">Run failures, missed schedules and unhealthy tenants show up here.</p>
          </div>
            : <ul className="max-h-96 divide-y divide-slate-100 overflow-y-auto">
              {items.map((event) => <Row key={event.id} event={event} busy={markOne.isPending} onRead={(id) => markOne.mutate(id)} onNavigate={() => setOpen(false)} />)}
            </ul>}

      <footer className="border-t border-slate-200 px-3 py-2 text-right">
        <Link to="/notifications/deliveries" className="text-xs font-medium text-blue-700 hover:text-blue-800" onClick={() => setOpen(false)}>Delivery history →</Link>
      </footer>
    </div>}
  </div>
}
