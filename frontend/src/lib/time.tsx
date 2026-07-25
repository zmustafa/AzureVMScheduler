import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api } from '../api'
import type { HealthResponse } from '../types'

/* ---------------------------------------------------------------- server clock */

let skewMs = 0

/** Current time in milliseconds, corrected for the offset between server and browser clocks. */
export function serverNow(): number {
  return Date.now() + skewMs
}

export function getClockSkewMs(): number {
  return skewMs
}

async function syncServerClock(): Promise<void> {
  try {
    const before = Date.now()
    const health = await api<HealthResponse>('/health')
    const after = Date.now()
    const serverMs = new Date(health.server_time).getTime()
    if (Number.isFinite(serverMs)) skewMs = serverMs - (before + (after - before) / 2)
  } catch {
    skewMs = 0
  }
}

/* ---------------------------------------------------------------- shared ticker */

type Subscriber = { cadence: number; last: number; notify: () => void }

const subscribers = new Set<Subscriber>()
let timer: ReturnType<typeof setInterval> | null = null

function ensureTimer() {
  if (timer !== null) return
  timer = setInterval(() => {
    const now = Date.now()
    for (const subscriber of subscribers) {
      if (now - subscriber.last >= subscriber.cadence - 50) {
        subscriber.last = now
        subscriber.notify()
      }
    }
  }, 1000)
}

function releaseTimer() {
  if (timer !== null && subscribers.size === 0) {
    clearInterval(timer)
    timer = null
  }
}

/** Re-render on the shared application ticker at the requested cadence. One interval serves every subscriber. */
export function useTick(cadence: number): number {
  const [, setVersion] = useState(0)
  useEffect(() => {
    const subscriber: Subscriber = { cadence, last: Date.now(), notify: () => setVersion((value) => value + 1) }
    subscribers.add(subscriber)
    ensureTimer()
    return () => {
      subscribers.delete(subscriber)
      releaseTimer()
    }
  }, [cadence])
  return cadence
}

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

function cadenceFor(deltaMs: number): number {
  const distance = Math.abs(deltaMs)
  if (distance < HOUR) return 1000
  if (distance < DAY) return 30_000
  return 5 * MINUTE
}

/* ---------------------------------------------------------------- formatting */

const pad = (value: number) => String(Math.floor(value)).padStart(2, '0')

export function formatInZone(iso?: string | null, timeZone?: string): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  // dateStyle/timeStyle may not be combined with timeZoneName, so use explicit components.
  const options: Intl.DateTimeFormatOptions = { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short', timeZone }
  try {
    return new Intl.DateTimeFormat(undefined, options).format(date)
  } catch {
    delete options.timeZone
    try {
      return new Intl.DateTimeFormat(undefined, options).format(date)
    } catch {
      return date.toISOString()
    }
  }
}

/** Compact absolute form used for far-away targets, e.g. "Tue 4 Aug, 07:00 EDT". */
export function formatAbsolute(iso?: string | null, timeZone?: string): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  const options: Intl.DateTimeFormatOptions = { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short', timeZone }
  try {
    return new Intl.DateTimeFormat('en-GB', options).format(date).replace(/,\s(\d{2}:\d{2})/, ', $1')
  } catch {
    delete options.timeZone
    try {
      return new Intl.DateTimeFormat('en-GB', options).format(date)
    } catch {
      return date.toISOString()
    }
  }
}

/** Short zone label such as "EDT" for a given instant. */
export function zoneLabel(timeZone: string, iso?: string | null): string {
  const date = iso ? new Date(iso) : new Date()
  try {
    const parts = new Intl.DateTimeFormat('en-US', { timeZone, timeZoneName: 'short' }).formatToParts(Number.isNaN(date.getTime()) ? new Date() : date)
    return parts.find((part) => part.type === 'timeZoneName')?.value ?? timeZone
  } catch {
    return timeZone
  }
}

/** Duration text without a prefix: "3d 4h", "1h 39m", "12m 04s", "41s". */
export function formatDuration(ms: number): string {
  const total = Math.max(Math.floor(Math.abs(ms) / 1000), 0)
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (days >= 1) return `${days}d ${hours}h`
  if (hours >= 1) return `${hours}h ${minutes}m`
  if (minutes >= 1) return `${minutes}m ${pad(seconds)}s`
  return `${seconds}s`
}

export function countdownText(target: string | null | undefined, now: number, timeZone?: string): string {
  if (!target) return '—'
  const targetMs = new Date(target).getTime()
  if (Number.isNaN(targetMs)) return '—'
  const delta = targetMs - now
  if (Math.abs(delta) < 2000) return 'starting now'
  if (delta < 0) return `overdue by ${formatDuration(delta)}`
  if (delta > 7 * DAY) return formatAbsolute(target, timeZone)
  return `in ${formatDuration(delta)}`
}

/** Live countdown for a target instant. Uses the shared ticker with an adaptive cadence. */
export function useCountdown(target: string | null | undefined, timeZone?: string): string {
  const targetMs = target ? new Date(target).getTime() : Number.NaN
  const cadence = Number.isNaN(targetMs) ? 5 * MINUTE : cadenceFor(targetMs - serverNow())
  useTick(cadence)
  return countdownText(target, serverNow(), timeZone)
}

/** Live elapsed text for something already running, e.g. "running 2m 18s". */
export function useElapsed(startedAt: string | null | undefined, prefix = 'running'): string {
  useTick(1000)
  if (!startedAt) return '—'
  const startedMs = new Date(startedAt).getTime()
  if (Number.isNaN(startedMs)) return '—'
  return `${prefix} ${formatDuration(serverNow() - startedMs)}`
}

/* ---------------------------------------------------------------- display timezone */

export type DisplayMode = 'schedule' | 'local' | 'utc'
const STORAGE_KEY = 'azureops.display-timezone'
const LOCAL_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'

type DisplayValue = {
  mode: DisplayMode
  setMode: (mode: DisplayMode) => void
  /** Resolve the zone to render in, given the row's own schedule timezone. */
  resolve: (scheduleTimezone?: string | null) => string
  /** Format an instant according to the active display mode. */
  format: (iso?: string | null, scheduleTimezone?: string | null) => string
  localZone: string
}

const DisplayTimezoneContext = createContext<DisplayValue | null>(null)

function readStoredMode(): DisplayMode {
  const stored = typeof localStorage === 'undefined' ? null : localStorage.getItem(STORAGE_KEY)
  return stored === 'local' || stored === 'utc' || stored === 'schedule' ? stored : 'schedule'
}

export function TimeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<DisplayMode>(readStoredMode)
  useEffect(() => { void syncServerClock() }, [])
  const value = useMemo<DisplayValue>(() => {
    const resolve = (scheduleTimezone?: string | null) => (mode === 'utc' ? 'UTC' : mode === 'local' ? LOCAL_ZONE : scheduleTimezone || LOCAL_ZONE)
    return {
      mode,
      setMode: (next: DisplayMode) => { setModeState(next); try { localStorage.setItem(STORAGE_KEY, next) } catch { /* storage unavailable */ } },
      resolve,
      format: (iso?: string | null, scheduleTimezone?: string | null) => formatInZone(iso, resolve(scheduleTimezone)),
      localZone: LOCAL_ZONE,
    }
  }, [mode])
  return <DisplayTimezoneContext.Provider value={value}>{children}</DisplayTimezoneContext.Provider>
}

export function useDisplayTimezone(): DisplayValue {
  const value = useContext(DisplayTimezoneContext)
  if (!value) throw new Error('TimeProvider is missing')
  return value
}

const MODES: { key: DisplayMode; label: string; hint: string }[] = [
  { key: 'schedule', label: 'Schedule', hint: 'Show every time in the timezone of its own schedule' },
  { key: 'local', label: 'Local', hint: `Show every time in your browser timezone (${LOCAL_ZONE})` },
  { key: 'utc', label: 'UTC', hint: 'Show every time in UTC' },
]

export function DisplayTimezoneSwitcher() {
  const { mode, setMode } = useDisplayTimezone()
  return <div className="inline-flex items-center gap-1 rounded-lg border border-slate-300 bg-white p-0.5" role="group" aria-label="Display timezone">
    {MODES.map((item) => <button
      key={item.key}
      type="button"
      title={item.hint}
      aria-pressed={mode === item.key}
      onClick={() => setMode(item.key)}
      className={`rounded-md px-2 py-1 text-xs font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure ${mode === item.key ? 'bg-blue-100 text-blue-800' : 'text-slate-600 hover:bg-slate-50'}`}
    >{item.label}</button>)}
  </div>
}
