import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import type { Connection, GeneralSettings, GroupNode, Paged, Schedule, ScheduleRun } from '../types'

/** Consistent "Name (tenant-id)" rendering for every Azure connection reference. */
export function connectionLabel(name?: string | null, tenantId?: string | null): string {
  if (!name) return 'Default tenant'
  return tenantId ? `${name} (${tenantId})` : name
}

export function connectionOptionLabel(connection: Connection): string {
  return connectionLabel(connection.display_name, connection.tenant_id)
}

const VM_RESOURCE_ID = /^\/subscriptions\/[0-9a-fA-F-]{36}\/resourceGroups\/[^/\s]+\/providers\/Microsoft\.Compute\/virtualMachines\/[^/\s]+$/

export function isVmResourceId(value: string): boolean {
  return VM_RESOURCE_ID.test(value.trim())
}

/**
 * Deep link to a resource in the Azure portal. The `@tenant` segment makes the portal switch
 * directories first, which matters when an operator is signed in to more than one.
 */
export function azurePortalUrl(resourceId: string, tenantId?: string | null): string {
  // Resource ids already start with "/", so the tenant segment must not add another slash.
  const resource = encodeURI(resourceId.trim())
  const directory = tenantId ? `@${encodeURIComponent(tenantId)}/` : ''
  return `https://portal.azure.com/#${directory}resource${resource}/overview`
}

export function splitResourceIds(text: string): string[] {
  return text.split(/[\r\n,;]+/).map((item) => item.trim()).filter(Boolean)
}

/** Split pasted VM names on newlines, commas, semicolons, tabs or spaces, de-duplicated case-insensitively. */
export function splitVmNames(text: string): string[] {
  const seen = new Set<string>()
  const output: string[] = []
  for (const raw of text.split(/[\r\n,;\t ]+/)) {
    const name = raw.trim()
    if (!name || seen.has(name.toLowerCase())) continue
    seen.add(name.toLowerCase())
    output.push(name)
  }
  return output
}

/** Flatten a group tree into depth-first order. */
export function flattenGroups(nodes: GroupNode[]): GroupNode[] {
  const output: GroupNode[] = []
  const walk = (items: GroupNode[]) => {
    for (const item of [...items].sort((a, b) => a.sequence - b.sequence || a.name.localeCompare(b.name))) {
      output.push(item)
      walk(item.children ?? [])
    }
  }
  walk(nodes)
  return output
}

export function findGroup(nodes: GroupNode[], id: string): GroupNode | undefined {
  return flattenGroups(nodes).find((item) => item.id === id)
}

/** Ids of a group plus everything beneath it — used to exclude invalid move targets. */
export function subtreeIds(node: GroupNode): Set<string> {
  const ids = new Set<string>()
  const walk = (item: GroupNode) => { ids.add(item.id); (item.children ?? []).forEach(walk) }
  walk(node)
  return ids
}

export function useGroupTree() {
  return useQuery({ queryKey: ['groups', 'tree'], queryFn: () => api<GroupNode[]>('/groups?shape=tree') })
}

export function useConnections() {
  return useQuery({ queryKey: ['connections'], queryFn: () => api<Connection[]>('/connections'), staleTime: 60_000 })
}

export function useGeneralSettings() {
  return useQuery({ queryKey: ['settings', 'general'], queryFn: () => api<GeneralSettings>('/settings/general'), staleTime: 300_000 })
}

/** Every schedule, keyed for quick target lookups on the applications board. */
export function useScheduleIndex() {
  return useQuery({
    queryKey: ['schedules', 'index'],
    queryFn: async () => {
      const page = await api<Paged<Schedule>>('/schedules?limit=1000')
      const byTarget = new Map<string, Schedule[]>()
      for (const item of page.items) {
        const bucket = byTarget.get(item.target_id) ?? []
        bucket.push(item)
        byTarget.set(item.target_id, bucket)
      }
      return { items: page.items, byTarget }
    },
  })
}

/** Latest run per schedule, used for "last run" chips. */
export function useLatestRuns() {
  return useQuery({
    queryKey: ['runs', 'latest'],
    queryFn: async () => {
      const page = await api<Paged<ScheduleRun>>('/runs?limit=200')
      const bySchedule = new Map<string, ScheduleRun>()
      for (const run of page.items) {
        if (run.schedule_id && !bySchedule.has(run.schedule_id)) bySchedule.set(run.schedule_id, run)
      }
      return bySchedule
    },
  })
}

export function staggerHint(vmCount: number, staggerSeconds: number): string {
  if (!vmCount) return 'No virtual machines resolve to this target yet.'
  if (staggerSeconds <= 0) return `${vmCount} VM${vmCount === 1 ? '' : 's'} start together.`
  const spreadSeconds = staggerSeconds * Math.max(vmCount - 1, 0)
  const spread = spreadSeconds >= 60 ? `~${Math.round(spreadSeconds / 60)} min` : `~${spreadSeconds}s`
  return `${vmCount} VM${vmCount === 1 ? '' : 's'} over ${spread}`
}
