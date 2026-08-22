import { Suspense, lazy, type ReactElement } from 'react'
import { Navigate, Outlet, Route, Routes } from 'react-router'
import { ProtectedLayout } from './components/Shell'
import { useCan } from './auth'
import { Loading } from './components/Ui'
import { LoginPage } from './pages/LoginPage'

// Only the sign-in screen and the shell are in the entry bundle. Everything behind the session is
// split per route, so a signed-out visitor does not download the whole administrative surface and
// an operator who never opens Settings never pays for it.
const DashboardPage = lazy(() => import('./pages/DashboardPage').then(m => ({ default: m.DashboardPage })))
const ApplicationsPage = lazy(() => import('./pages/ApplicationsPage').then(m => ({ default: m.ApplicationsPage })))
const LocateVmsPage = lazy(() => import('./pages/LocateVmsPage').then(m => ({ default: m.LocateVmsPage })))
const GroupDetailPage = lazy(() => import('./pages/GroupDetailPage').then(m => ({ default: m.GroupDetailPage })))
const VmsPage = lazy(() => import('./pages/VmsPage').then(m => ({ default: m.VmsPage })))
const SchedulesPage = lazy(() => import('./pages/SchedulesPage').then(m => ({ default: m.SchedulesPage })))
const ScheduleDetailPage = lazy(() => import('./pages/ScheduleDetailPage').then(m => ({ default: m.ScheduleDetailPage })))
const TimelinePage = lazy(() => import('./pages/TimelinePage').then(m => ({ default: m.TimelinePage })))
const RunsPage = lazy(() => import('./pages/RunsPage').then(m => ({ default: m.RunsPage })))
const RunDetailPage = lazy(() => import('./pages/RunDetailPage').then(m => ({ default: m.RunDetailPage })))
const ImportPage = lazy(() => import('./pages/ImportPage').then(m => ({ default: m.ImportPage })))
const SettingsPage = lazy(() => import('./pages/SettingsPage').then(m => ({ default: m.SettingsPage })))
const AccessControlPage = lazy(() => import('./pages/AccessControlPage').then(m => ({ default: m.AccessControlPage })))
const TenantsPage = lazy(() => import('./pages/TenantsPage').then(m => ({ default: m.TenantsPage })))
const ConnectorsPage = lazy(() => import('./pages/ConnectorsPage').then(m => ({ default: m.ConnectorsPage })))
const NotificationRulesPage = lazy(() => import('./pages/NotificationRulesPage').then(m => ({ default: m.NotificationRulesPage })))
const DeliveriesPage = lazy(() => import('./pages/DeliveriesPage').then(m => ({ default: m.DeliveriesPage })))
const AuditPage = lazy(() => import('./pages/AuditPage').then(m => ({ default: m.AuditPage })))

/** Route-level permission gate; the sidebar hides the same entries. */
function Require({ permission, children }: { permission: string; children: ReactElement }) {
  return useCan(permission) ? children : <Navigate to="/" replace />
}

export default function App() {
  return <Routes>
    <Route path="/login" element={<LoginPage/>}/>
    <Route element={<ProtectedLayout/>}>
      <Route element={<Suspense fallback={<Loading/>}><Outlet/></Suspense>}>
        <Route index element={<DashboardPage/>}/>
        <Route path="applications" element={<ApplicationsPage/>}/>
        <Route path="applications/locate" element={<LocateVmsPage/>}/>
        <Route path="applications/:groupId" element={<GroupDetailPage/>}/>
        <Route path="vms" element={<VmsPage/>}/>
        <Route path="schedules" element={<SchedulesPage/>}/>
        <Route path="schedules/:id" element={<ScheduleDetailPage/>}/>
        <Route path="timeline" element={<TimelinePage/>}/>
        <Route path="runs" element={<RunsPage/>}/>
        <Route path="runs/:id" element={<RunDetailPage/>}/>
        <Route path="import" element={<ImportPage/>}/>
        <Route path="settings" element={<SettingsPage/>}/>
        <Route path="settings/access" element={<Require permission="users.manage"><AccessControlPage/></Require>}/>
        {/* Access control used to live at the top level; keep old links and bookmarks working. */}
        <Route path="access" element={<Navigate to="/settings/access" replace/>}/>
        <Route path="settings/tenants" element={<TenantsPage/>}/>
        <Route path="settings/connectors" element={<Require permission="connectors.read"><ConnectorsPage/></Require>}/>
        <Route path="settings/notifications" element={<Require permission="notifications.read"><NotificationRulesPage/></Require>}/>
        <Route path="notifications/deliveries" element={<Require permission="notifications.read"><DeliveriesPage/></Require>}/>
        <Route path="audit" element={<AuditPage/>}/>
      </Route>
    </Route>
    <Route path="*" element={<Navigate to="/" replace/>}/>
  </Routes>
}
