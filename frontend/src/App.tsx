import { Navigate, Route, Routes } from 'react-router-dom'
import { ProtectedLayout } from './components/Shell'
import { useCan } from './auth'
import { LoginPage } from './pages/LoginPage'
import { DashboardPage } from './pages/DashboardPage'
import { ApplicationsPage } from './pages/ApplicationsPage'
import { LocateVmsPage } from './pages/LocateVmsPage'
import { GroupDetailPage } from './pages/GroupDetailPage'
import { VmsPage } from './pages/VmsPage'
import { SchedulesPage } from './pages/SchedulesPage'
import { ScheduleDetailPage } from './pages/ScheduleDetailPage'
import { TimelinePage } from './pages/TimelinePage'
import { RunsPage } from './pages/RunsPage'
import { RunDetailPage } from './pages/RunDetailPage'
import { ImportPage } from './pages/ImportPage'
import { SettingsPage } from './pages/SettingsPage'
import { AccessControlPage } from './pages/AccessControlPage'
import { TenantsPage } from './pages/TenantsPage'
import { ConnectorsPage } from './pages/ConnectorsPage'
import { NotificationRulesPage } from './pages/NotificationRulesPage'
import { DeliveriesPage } from './pages/DeliveriesPage'
import { AuditPage } from './pages/AuditPage'
import type { ReactElement } from 'react'

/** Route-level permission gate; the sidebar hides the same entries. */
function Require({ permission, children }: { permission: string; children: ReactElement }) {
  return useCan(permission) ? children : <Navigate to="/" replace />
}

export default function App() {
  return <Routes>
    <Route path="/login" element={<LoginPage/>}/>
    <Route element={<ProtectedLayout/>}>
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
    <Route path="*" element={<Navigate to="/" replace/>}/>
  </Routes>
}
