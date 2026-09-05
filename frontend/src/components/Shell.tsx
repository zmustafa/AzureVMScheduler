import { useEffect, useState } from 'react'
import { NavLink, Navigate, Outlet, useLocation } from 'react-router'
import { Activity, BellRing, CalendarClock, ChevronDown, Cloud, FileClock, Gauge, Layers, ListTree, Menu, Plug, ScrollText, Server, Settings, ShieldCheck, SlidersHorizontal, X } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useAuth, hasPermission } from '../auth'
import { DisplayTimezoneSwitcher } from '../lib/time'
import { useUnreadCount } from '../lib/notify'
import { ForcePasswordChangePage } from '../pages/ForcePasswordChangePage'
import { NotificationBell } from './NotificationBell'
import { Loading } from './Ui'

type NavItem = { label: string; to: string; icon: LucideIcon; permission: string | null; children?: NavItem[] }

/**
 * Everything that configures the installation lives under Settings, so the sidebar stays short and
 * the daily-use pages are not buried among admin screens. Child routes sit under `/settings/*` so
 * the URL matches the menu.
 */
const nav: NavItem[] = [
  { label: 'Overview', to: '/', icon: Gauge, permission: 'dashboard.read' },
  { label: 'Applications', to: '/applications', icon: Layers, permission: 'groups.read' },
  { label: 'Virtual machines', to: '/vms', icon: Server, permission: 'vms.read' },
  { label: 'Schedules', to: '/schedules', icon: CalendarClock, permission: 'schedules.read' },
  { label: 'Timeline', to: '/timeline', icon: ListTree, permission: 'schedules.read' },
  { label: 'Runs', to: '/runs', icon: Activity, permission: 'runs.read' },
  { label: 'Import VMs', to: '/import', icon: FileClock, permission: 'imports.write' },
  { label: 'Audit log', to: '/audit', icon: ScrollText, permission: 'audit.read' },
  {
    label: 'Settings', to: '/settings', icon: Settings, permission: null, children: [
      { label: 'General', to: '/settings', icon: SlidersHorizontal, permission: null },
      { label: 'Azure Tenants', to: '/settings/tenants', icon: Cloud, permission: 'connections.manage' },
      { label: 'Connectors', to: '/settings/connectors', icon: Plug, permission: 'connectors.read' },
      { label: 'Notifications', to: '/settings/notifications', icon: BellRing, permission: 'notifications.read' },
      { label: 'Access control', to: '/settings/access', icon: ShieldCheck, permission: 'users.manage' },
    ],
  },
]

const NAV_OPEN_KEY = 'azureops.nav-open'

/** Every nav path, parents and children alike, used to decide which links must match exactly. */
const allPaths = nav.flatMap(item => [item.to, ...(item.children ?? []).map(child => child.to)])

/**
 * Match exactly when another nav entry lives beneath this path, so a parent never lights up for
 * its children — `/settings` must not look selected while you are on `/settings/tenants`.
 *
 * Detail routes such as `/schedules/{id}` have no nav entry of their own, so their parent keeps
 * the highlight, which is what you want.
 */
const isExactMatch = (to: string) => to === '/' || allPaths.some(other => other !== to && other.startsWith(`${to}/`))

const visible = (item: NavItem, permissions: readonly string[] | undefined) =>
  !item.permission || permissions?.includes('*') || permissions?.includes(item.permission)

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure ${isActive ? 'bg-blue-100 text-blue-800 ring-1 ring-blue-200' : 'text-slate-600 hover:bg-white hover:text-slate-900'}`

/**
 * A nav entry with children. The group opens automatically when you are on one of its pages, so a
 * deep link never lands you in a collapsed menu; after that your own choice is remembered.
 */
function NavGroup({ item, onNavigate }: { item: NavItem; onNavigate: () => void }) {
  const { pathname } = useLocation()
  const children = item.children ?? []
  const holdsCurrentPage = children.some(child => pathname === child.to || pathname.startsWith(`${child.to}/`))
  const [open, setOpen] = useState(() => holdsCurrentPage || localStorage.getItem(NAV_OPEN_KEY) === item.to)
  useEffect(() => { if (holdsCurrentPage) setOpen(true) }, [holdsCurrentPage])
  const toggle = () => {
    setOpen(!open)
    try { localStorage.setItem(NAV_OPEN_KEY, open ? '' : item.to) } catch { /* storage unavailable */ }
  }
  const Icon = item.icon
  return <div>
    <button type="button" onClick={toggle} aria-expanded={open} className={`${linkClass({ isActive: false })} w-full`}>
      <Icon size={18} />{item.label}
      <ChevronDown size={16} className={`ml-auto transition-transform ${open ? '' : '-rotate-90'}`} />
    </button>
    {open && <div className="mt-1 space-y-1 border-l border-slate-200 pl-3 ml-5">
      {children.map(child => <NavLink key={child.to} to={child.to} end={isExactMatch(child.to)} onClick={onNavigate} className={linkClass}>
        <child.icon size={16} />{child.label}
      </NavLink>)}
    </div>}
  </div>
}

export function ProtectedLayout() {
  const { user, loading, logout } = useAuth()
  const [open, setOpen] = useState(false)
  const canSeeNotifications = hasPermission(user, 'notifications.read')
  // Hooks run before the guards below, so the poll is disabled for a principal the server will
  // refuse anyway — otherwise the forced password-change screen sits behind a 403 poll loop.
  const unread = useUnreadCount(!!user && !user.must_change_password && canSeeNotifications)
  if (loading) return <main className="mx-auto max-w-lg p-8"><Loading /></main>
  if (!user) return <Navigate to="/login" replace />
  // The server refuses every path outside a small allowlist while this flag is set, so the shell
  // and its pages would only render 403s. Show the one screen that can clear the flag instead.
  if (user.must_change_password) return <ForcePasswordChangePage />
  // Hide a group entirely once every page inside it is out of reach, rather than leaving an empty menu.
  const allowed = nav
    .filter(item => visible(item, user.permissions))
    .map(item => item.children ? { ...item, children: item.children.filter(child => visible(child, user.permissions)) } : item)
    .filter(item => !item.children || item.children.length > 0)
  return <div className="min-h-screen bg-[radial-gradient(circle_at_top_right,rgba(0,120,212,.08),transparent_35%)]">
    <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-slate-200 bg-white/90 px-4 backdrop-blur">
      <button className="btn-secondary !p-2 lg:hidden" onClick={() => setOpen(!open)} aria-label="Toggle navigation">{open ? <X size={20}/> : <Menu size={20}/>}</button>
      <Brand />
      <div className="ml-auto flex items-center gap-2">{canSeeNotifications && <NotificationBell unread={unread.data ?? 0} />}<DisplayTimezoneSwitcher /></div>
    </header>
    <aside className={`fixed bottom-0 left-0 top-16 z-20 w-64 border-r border-slate-200 bg-sky-50/95 p-4 backdrop-blur transition-transform lg:translate-x-0 ${open ? 'translate-x-0' : '-translate-x-full'}`}>
      <nav className="space-y-1" aria-label="Primary">{allowed.map(item => item.children
        ? <NavGroup key={item.to} item={item} onNavigate={() => setOpen(false)} />
        : <NavLink key={item.to} to={item.to} end={isExactMatch(item.to)} onClick={() => setOpen(false)} className={linkClass}><item.icon size={18}/>{item.label}</NavLink>)}</nav>
      <div className="absolute bottom-4 left-4 right-4 rounded-lg border border-slate-200 bg-white p-3 shadow-sm"><p className="truncate text-sm font-medium text-slate-900">{user.username}</p><p className="text-xs capitalize text-slate-500">{user.role}</p><button className="mt-3 text-xs font-medium text-blue-700 hover:text-blue-800" onClick={() => void logout()}>Sign out</button></div>
    </aside>
    {open && <button className="fixed inset-0 z-10 bg-black/60 lg:hidden" onClick={() => setOpen(false)} aria-label="Close navigation" />}
    <main className="min-h-screen p-4 sm:p-6 lg:ml-64 lg:p-8"><div className="mx-auto max-w-7xl"><Outlet /></div></main>
  </div>
}
/** Product title in the header. Sized to the sidebar column so it reads as the app's identity. */
function Brand() {
  return <NavLink to="/" className="flex items-center gap-3 rounded-lg pr-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure lg:w-56">
    <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-gradient-to-br from-sky-500 to-blue-700 text-white shadow-md shadow-blue-200"><Activity size={20}/></span>
    <span className="min-w-0">
      <span className="block truncate font-bold tracking-tight text-slate-900">Azure VM Scheduler</span>
      <span className="block text-[10px] uppercase tracking-[.18em] text-slate-500">Start &amp; stop orchestration</span>
    </span>
  </NavLink>
}
