import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Mail, MessagesSquare, Slack, Ticket, Webhook, type LucideIcon } from 'lucide-react'
import { api } from '../api'
import { formatDuration, serverNow, useTick } from './time'
import type { ConnectorsResponse, DeliveryStatus, Severity } from '../types'

/* ---------------------------------------------------------------- severity */

export const SEVERITIES: Severity[] = ['info', 'warning', 'error', 'critical']

export const SEVERITY_META: Record<Severity, { label: string; dot: string; tone: 'info' | 'warn' | 'danger' }> = {
  info: { label: 'Info', dot: 'bg-sky-500', tone: 'info' },
  warning: { label: 'Warning', dot: 'bg-amber-500', tone: 'warn' },
  error: { label: 'Error', dot: 'bg-rose-500', tone: 'danger' },
  critical: { label: 'Critical', dot: 'bg-rose-700', tone: 'danger' },
}

export function severityMeta(value: string) {
  return SEVERITY_META[value as Severity] ?? SEVERITY_META.info
}

/** A dot is never the only signal — callers always pair it with the label text. */
export function SeverityDot({ severity }: { severity: string }) {
  const meta = severityMeta(severity)
  return <span className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${meta.dot}`} role="img" aria-label={`Severity: ${meta.label}`} />
}

/* ---------------------------------------------------------------- event types */

const EVENT_LABELS: Record<string, string> = {
  'run.succeeded': 'Run succeeded',
  'run.partially_failed': 'Run partially failed',
  'run.failed': 'Run failed',
  'run.timed_out': 'Run timed out',
  'vm.start_failed': 'VM start failed',
  'vm.start_timed_out': 'VM start timed out',
  'vm.start_skipped': 'VM start skipped',
  'vm.stop_failed': 'VM stop failed',
  'vm.stop_timed_out': 'VM stop timed out',
  'vm.stop_skipped': 'VM stop skipped',
  'schedule.missed': 'Schedule missed',
  'connection.unhealthy': 'Tenant connection unhealthy',
  'digest.daily': 'Daily digest',
  'connector.test': 'Connector test',
}

export function eventLabel(type: string): string {
  return EVENT_LABELS[type] ?? type.replaceAll('.', ' · ').replaceAll('_', ' ')
}

/** Events that the backend can raise once per virtual machine rather than once per wave. */
export const PER_VM_EVENTS = new Set(['vm.start_failed', 'vm.start_timed_out', 'vm.start_skipped', 'vm.stop_failed', 'vm.stop_timed_out', 'vm.stop_skipped'])

/* ---------------------------------------------------------------- connector types */

export const CONNECTOR_ICONS: Record<string, LucideIcon> = {
  email: Mail,
  teams: MessagesSquare,
  slack: Slack,
  servicenow: Ticket,
  webhook: Webhook,
}

export function connectorIcon(type: string): LucideIcon {
  return CONNECTOR_ICONS[type] ?? Webhook
}

/** Gallery grouping. Any type the backend adds later falls through to "More". */
export const CONNECTOR_CATEGORIES: { title: string; blurb: string; types: string[] }[] = [
  { title: 'Messaging & ChatOps', blurb: 'Tell people what happened, where they already work.', types: ['email', 'teams', 'slack'] },
  { title: 'Ticketing & ITSM', blurb: 'Open and close incidents automatically.', types: ['servicenow'] },
  { title: 'Custom', blurb: 'Forward the raw event anywhere you like.', types: ['webhook'] },
]

export function modeLabel(type: string, mode: string): string {
  if (type === 'email') return mode === 'm365_graph' ? 'Microsoft 365 Graph' : 'SMTP'
  return mode.replaceAll('_', ' ')
}

/* ---------------------------------------------------------------- delivery status */

export const DELIVERY_TONES: Record<DeliveryStatus, 'neutral' | 'info' | 'success' | 'warn' | 'danger'> = {
  pending: 'info',
  sent: 'success',
  failed: 'danger',
  skipped: 'neutral',
}

export function deliveryTone(status: string) {
  return DELIVERY_TONES[status as DeliveryStatus] ?? 'neutral'
}

/* ---------------------------------------------------------------- queries */

export const CONNECTORS_KEY = ['connectors'] as const
export const NOTIFICATION_RULES_KEY = ['notification-rules'] as const
export const UNREAD_KEY = ['notifications', 'unread'] as const

export function useConnectorCatalog() {
  return useQuery({ queryKey: CONNECTORS_KEY, queryFn: () => api<ConnectorsResponse>('/connectors') })
}

/**
 * Unread badge count. Polling is suspended whenever the tab is hidden, and a fresh
 * read is forced the moment it becomes visible again.
 */
export function useUnreadCount(enabled: boolean) {
  const client = useQueryClient()
  const query = useQuery({
    queryKey: UNREAD_KEY,
    enabled,
    queryFn: () => api<{ count: number }>('/notifications/unread-count').then((data) => data.count),
    refetchInterval: () => (document.visibilityState === 'visible' ? 60_000 : false),
    refetchIntervalInBackground: false,
  })
  useEffect(() => {
    if (!enabled) return
    const onVisible = () => { if (document.visibilityState === 'visible') void client.invalidateQueries({ queryKey: UNREAD_KEY }) }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [client, enabled])
  return query
}

/* ---------------------------------------------------------------- relative time */

export function relativeText(iso?: string | null, now: number = Date.now()): string {
  if (!iso) return '—'
  const ms = new Date(iso).getTime()
  if (Number.isNaN(ms)) return '—'
  const delta = now - ms
  if (delta < 45_000) return 'just now'
  return delta >= 0 ? `${formatDuration(delta)} ago` : `in ${formatDuration(-delta)}`
}

/** Live "3m 12s ago" text driven by the shared application ticker. */
export function useRelativeText(iso?: string | null): string {
  useTick(30_000)
  return relativeText(iso, serverNow())
}
