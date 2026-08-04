import { useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router'
import { KeyRound, MonitorSmartphone, ShieldBan, ShieldCheck, Trash2, Users, UsersRound } from 'lucide-react'

import { api, json } from '../api'
import { useAuth } from '../auth'
import { useDisplayTimezone } from '../lib/time'
import { ConfirmDialog } from '../components/Overlay'
import { Callout, CopyRow, SetupGuide, Step } from '../components/Help'
import { Chip, ErrorNotice, Field, Loading, PageHeader } from '../components/Ui'
import type {
  AccessGroup,
  AccessRole,
  AccessUser,
  AuthPolicies,
  IdentityProvider,
  IdpTestResult,
  IpAccessState,
  IpAllowlistMode,
  IpAllowlistScope,
  IpBlockEvent,
  IpRule,
  LoginSession,
  PermissionCatalogItem,
  ProviderType,
} from '../types'

type Tab = 'users' | 'roles' | 'groups' | 'sessions' | 'policies' | 'firewall' | 'sso'

const TABS: { key: Tab; label: string; icon: typeof Users }[] = [
  { key: 'users', label: 'Users', icon: Users },
  { key: 'roles', label: 'Roles', icon: ShieldCheck },
  { key: 'groups', label: 'Access groups', icon: UsersRound },
  { key: 'sessions', label: 'Sessions', icon: MonitorSmartphone },
  { key: 'policies', label: 'Policies', icon: KeyRound },
  { key: 'firewall', label: 'IP access', icon: ShieldBan },
  { key: 'sso', label: 'Sign-in & SSO', icon: KeyRound },
]

const keys = {
  users: ['access', 'users'],
  roles: ['access', 'roles'],
  groups: ['access', 'access-groups'],
  sessions: ['access', 'sessions'],
  policies: ['access', 'policies'],
  providers: ['access', 'identity-providers'],
  permissions: ['access', 'permissions'],
  ipAccess: ['access', 'ip-rules'],
  ipBlocks: ['access', 'ip-blocks'],
} as const

function useInvalidate() {
  const client = useQueryClient()
  return (key: readonly string[]) => void client.invalidateQueries({ queryKey: [...key] })
}

/** Roles and groups are referenced by id everywhere; this keeps the lookups in one place. */
function useRoles() {
  return useQuery({ queryKey: [...keys.roles], queryFn: () => api<AccessRole[]>('/access/roles') })
}

function roleNames(roles: AccessRole[], ids: string[]): string[] {
  const byId = new Map(roles.map((role) => [role.id, role.name]))
  return ids.map((id) => byId.get(id) ?? 'unknown').sort()
}

// -- users ---------------------------------------------------------------

function UsersTab() {
  const invalidate = useInvalidate()
  const { format } = useDisplayTimezone()
  const users = useQuery({ queryKey: [...keys.users], queryFn: () => api<AccessUser[]>('/access/users') })
  const roles = useRoles()
  const groups = useQuery({ queryKey: [...keys.groups], queryFn: () => api<AccessGroup[]>('/access/access-groups') })
  const [editing, setEditing] = useState<AccessUser | 'new' | null>(null)
  const [removing, setRemoving] = useState<AccessUser | null>(null)

  const after = () => { invalidate(keys.users); invalidate(keys.groups); setEditing(null); setRemoving(null) }
  const save = useMutation({
    mutationFn: (input: { id?: string; body: unknown }) => input.id
      ? api<AccessUser>(`/access/users/${input.id}`, json('PATCH', input.body))
      : api<AccessUser>('/access/users', json('POST', input.body)),
    onSuccess: after,
  })
  const remove = useMutation({ mutationFn: (id: string) => api(`/access/users/${id}`, json('DELETE')), onSuccess: after })
  const resetPassword = useMutation({ mutationFn: (input: { id: string; password: string }) => api(`/access/users/${input.id}/reset-password`, json('POST', { new_password: input.password })), onSuccess: () => invalidate(keys.users) })
  const revokeAll = useMutation({ mutationFn: (id: string) => api(`/access/users/${id}/revoke-sessions`, json('POST')), onSuccess: () => invalidate(keys.sessions) })

  if (users.isLoading || roles.isLoading) return <Loading />
  const error = users.error ?? save.error ?? remove.error ?? resetPassword.error ?? revokeAll.error

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="muted">Accounts, their roles, and the access groups they belong to.</p>
      {!editing && <button type="button" className="btn-primary" onClick={() => setEditing('new')}>New user</button>}
    </div>
    {error && <ErrorNotice error={error} />}

    {editing && <UserEditor
      user={editing === 'new' ? null : editing}
      roles={roles.data ?? []}
      groups={groups.data ?? []}
      busy={save.isPending}
      onCancel={() => setEditing(null)}
      onSave={(body) => save.mutate({ id: editing === 'new' ? undefined : editing.id, body })}
    />}

    <div className="surface divide-y divide-slate-200">
      {(users.data ?? []).map((user) => <div key={user.id} className="grid gap-3 p-4 md:grid-cols-[1.4fr_1.4fr_auto] md:items-center">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 font-medium text-slate-900">
            {user.username}
            {user.is_break_glass && <Chip tone="warn">break-glass</Chip>}
            {user.disabled && <Chip tone="danger">Disabled</Chip>}
            {user.locked_until && <Chip tone="warn">Locked</Chip>}
          </p>
          <p className="truncate text-xs text-slate-500">
            {user.email || user.auth_source} · last login {user.last_login_at ? format(user.last_login_at) : 'never'}
          </p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {roleNames(roles.data ?? [], user.role_ids).map((name) => <Chip key={name} tone="info">{name}</Chip>)}
          {user.role_ids.length === 0 && <Chip tone="neutral">No role</Chip>}
          {user.access_group_ids.length > 0 && <Chip tone="accent">{user.access_group_ids.length} group{user.access_group_ids.length === 1 ? '' : 's'}</Chip>}
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <button type="button" className="btn-secondary !py-1" onClick={() => setEditing(user)}>Edit</button>
          <button type="button" className="btn-secondary !py-1" onClick={() => {
            const password = window.prompt(`New temporary password for ${user.username}`)
            if (password) resetPassword.mutate({ id: user.id, password })
          }}>Reset password</button>
          <button type="button" className="btn-secondary !py-1" onClick={() => revokeAll.mutate(user.id)}>Sign out</button>
          <button type="button" className="btn-danger !px-2 !py-1" disabled={user.is_break_glass} title={user.is_break_glass ? 'The break-glass account cannot be deleted' : 'Delete user'} onClick={() => setRemoving(user)}><Trash2 size={14} /></button>
        </div>
      </div>)}
    </div>

    <ConfirmDialog
      open={removing !== null}
      title={`Delete ${removing?.username ?? ''}?`}
      confirmLabel="Delete user"
      busy={remove.isPending}
      onCancel={() => setRemoving(null)}
      onConfirm={() => removing && remove.mutate(removing.id)}
    >
      <p>Their sessions end immediately and their run history keeps the original actor id. This cannot be undone.</p>
    </ConfirmDialog>
  </div>
}

function UserEditor({ user, roles, groups, busy, onCancel, onSave }: {
  user: AccessUser | null
  roles: AccessRole[]
  groups: AccessGroup[]
  busy: boolean
  onCancel: () => void
  onSave: (body: Record<string, unknown>) => void
}) {
  const [username, setUsername] = useState(user?.username ?? '')
  const [email, setEmail] = useState(user?.email ?? '')
  const [password, setPassword] = useState('')
  const [roleIds, setRoleIds] = useState<string[]>(user?.role_ids ?? [])
  const [groupIds, setGroupIds] = useState<string[]>(user?.access_group_ids ?? [])
  const [disabled, setDisabled] = useState(user?.disabled ?? false)

  const toggle = (list: string[], id: string) => list.includes(id) ? list.filter((item) => item !== id) : [...list, id]
  const submit = () => onSave(user
    ? { email: email || null, role_ids: roleIds, access_group_ids: groupIds, disabled }
    : { username: username.trim(), email: email || null, password, role_ids: roleIds, access_group_ids: groupIds })

  return <section className="card space-y-4">
    <h3 className="font-semibold text-slate-900">{user ? `Edit ${user.username}` : 'New user'}</h3>
    <div className="grid gap-4 md:grid-cols-2">
      {!user && <Field label="Username"><input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="off" required /></Field>}
      <Field label="Email"><input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="off" /></Field>
      {!user && <Field label="Temporary password" hint="The user is asked to change it at first sign-in."><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" required /></Field>}
    </div>

    <Field label="Roles" hint="Permissions are the union of these roles and any granted by an access group.">
      <div className="flex flex-wrap gap-1.5">
        {roles.map((role) => <Toggle key={role.id} active={roleIds.includes(role.id)} onClick={() => setRoleIds(toggle(roleIds, role.id))}>{role.name}</Toggle>)}
      </div>
    </Field>

    {groups.length > 0 && <Field label="Access groups">
      <div className="flex flex-wrap gap-1.5">
        {groups.map((group) => <Toggle key={group.id} active={groupIds.includes(group.id)} onClick={() => setGroupIds(toggle(groupIds, group.id))}>{group.name}</Toggle>)}
      </div>
    </Field>}

    {user && !user.is_break_glass && <label className="flex items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
      <input className="!w-auto" type="checkbox" checked={disabled} onChange={(event) => setDisabled(event.target.checked)} />
      <span><span className="block text-sm font-medium text-slate-800">Account disabled</span><span className="text-xs text-slate-500">They cannot sign in, and existing sessions end on their next request.</span></span>
    </label>}

    <div className="flex gap-3 border-t border-slate-200 pt-4">
      <button type="button" className="btn-primary" disabled={busy || (!user && (!username.trim() || !password))} onClick={submit}>{busy ? 'Saving…' : 'Save'}</button>
      <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
    </div>
  </section>
}

/** Chip-style multi-select toggle used for roles, groups and permissions. */
function Toggle({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return <button
    type="button"
    aria-pressed={active}
    onClick={onClick}
    className={`rounded-full border px-3 py-1 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure ${active ? 'border-blue-500 bg-blue-600 text-white' : 'border-slate-300 bg-white text-slate-600 hover:bg-slate-50'}`}
  >{children}</button>
}

// -- roles ---------------------------------------------------------------

function RolesTab() {
  const invalidate = useInvalidate()
  const roles = useRoles()
  const catalog = useQuery({ queryKey: [...keys.permissions], queryFn: () => api<PermissionCatalogItem[]>('/access/permissions') })
  const [editing, setEditing] = useState<AccessRole | 'new' | null>(null)
  const [removing, setRemoving] = useState<AccessRole | null>(null)

  const after = () => { invalidate(keys.roles); invalidate(keys.users); setEditing(null); setRemoving(null) }
  const save = useMutation({
    mutationFn: (input: { id?: string; body: unknown }) => input.id
      ? api<AccessRole>(`/access/roles/${input.id}`, json('PATCH', input.body))
      : api<AccessRole>('/access/roles', json('POST', input.body)),
    onSuccess: after,
  })
  const remove = useMutation({ mutationFn: (id: string) => api(`/access/roles/${id}`, json('DELETE')), onSuccess: after })

  const sections = useMemo(() => {
    const grouped = new Map<string, PermissionCatalogItem[]>()
    for (const item of catalog.data ?? []) grouped.set(item.group, [...(grouped.get(item.group) ?? []), item])
    return [...grouped.entries()]
  }, [catalog.data])

  if (roles.isLoading) return <Loading />

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="muted">Roles bundle permissions. Assign them to a user directly, or through an access group.</p>
      {!editing && <button type="button" className="btn-primary" onClick={() => setEditing('new')}>New role</button>}
    </div>
    {(roles.error || save.error || remove.error) && <ErrorNotice error={roles.error ?? save.error ?? remove.error} />}

    {editing && <RoleEditor
      role={editing === 'new' ? null : editing}
      sections={sections}
      busy={save.isPending}
      onCancel={() => setEditing(null)}
      onSave={(body) => save.mutate({ id: editing === 'new' ? undefined : editing.id, body })}
    />}

    <div className="space-y-2">
      {(roles.data ?? []).map((role) => <div key={role.id} className="surface flex flex-wrap items-center justify-between gap-3 p-4">
        <div className="min-w-0">
          <p className="flex items-center gap-2 font-medium text-slate-900">{role.name}{role.is_system && <Chip tone="neutral">built-in</Chip>}</p>
          <p className="text-xs text-slate-500">{role.description || '—'} · {role.permissions.includes('*') ? 'every permission' : `${role.permissions.length} permission${role.permissions.length === 1 ? '' : 's'}`}</p>
        </div>
        <div className="flex gap-2">
          <button type="button" className="btn-secondary !py-1" onClick={() => setEditing(role)}>{role.is_system ? 'View' : 'Edit'}</button>
          {!role.is_system && <button type="button" className="btn-danger !py-1" onClick={() => setRemoving(role)}>Delete</button>}
        </div>
      </div>)}
    </div>

    <ConfirmDialog
      open={removing !== null}
      title={`Delete the ${removing?.name ?? ''} role?`}
      confirmLabel="Delete role"
      busy={remove.isPending}
      onCancel={() => setRemoving(null)}
      onConfirm={() => removing && remove.mutate(removing.id)}
    >
      <p>It is removed from every user and access group that references it. Anyone left without another role loses access.</p>
    </ConfirmDialog>
  </div>
}

function RoleEditor({ role, sections, busy, onCancel, onSave }: {
  role: AccessRole | null
  sections: [string, PermissionCatalogItem[]][]
  busy: boolean
  onCancel: () => void
  onSave: (body: Record<string, unknown>) => void
}) {
  const [name, setName] = useState(role?.name ?? '')
  const [description, setDescription] = useState(role?.description ?? '')
  const [selected, setSelected] = useState<string[]>(role?.permissions ?? [])
  const everything = selected.includes('*')

  return <section className="card space-y-4">
    <h3 className="font-semibold text-slate-900">{role ? `${role.is_system ? 'Built-in role' : 'Edit'}: ${role.name}` : 'New role'}</h3>
    {role?.is_system && <Callout tone="info" title="Built-in role">
      Its name and permissions are owned by the application so a new feature is never unusable by an administrator. You can read them here; create a custom role to define your own.
    </Callout>}

    <div className="grid gap-4 md:grid-cols-2">
      <Field label="Name"><input value={name} onChange={(event) => setName(event.target.value)} disabled={role?.is_system} /></Field>
      <Field label="Description"><input value={description} onChange={(event) => setDescription(event.target.value)} disabled={role?.is_system} /></Field>
    </div>

    {everything
      ? <Callout tone="warn" title="Every permission">This role holds the <code>*</code> wildcard, so it automatically gains any capability added to the product later.</Callout>
      : <div className="space-y-3">
        {sections.map(([section, items]) => <div key={section}>
          <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">{section}</p>
          <div className="flex flex-wrap gap-1.5">
            {items.map((item) => <Toggle
              key={item.key}
              active={selected.includes(item.key)}
              onClick={() => !role?.is_system && setSelected(selected.includes(item.key) ? selected.filter((value) => value !== item.key) : [...selected, item.key])}
            >{item.label}</Toggle>)}
          </div>
        </div>)}
      </div>}

    <div className="flex gap-3 border-t border-slate-200 pt-4">
      {!role?.is_system && <button type="button" className="btn-primary" disabled={busy || !name.trim()} onClick={() => onSave({ name: name.trim(), description, permissions: selected })}>{busy ? 'Saving…' : 'Save role'}</button>}
      <button type="button" className="btn-secondary" onClick={onCancel}>{role?.is_system ? 'Close' : 'Cancel'}</button>
    </div>
  </section>
}

// -- access groups -------------------------------------------------------

function GroupsTab() {
  const invalidate = useInvalidate()
  const groups = useQuery({ queryKey: [...keys.groups], queryFn: () => api<AccessGroup[]>('/access/access-groups') })
  const roles = useRoles()
  const [editing, setEditing] = useState<AccessGroup | 'new' | null>(null)
  const [removing, setRemoving] = useState<AccessGroup | null>(null)

  const after = () => { invalidate(keys.groups); invalidate(keys.users); setEditing(null); setRemoving(null) }
  const save = useMutation({
    mutationFn: (input: { id?: string; body: unknown }) => input.id
      ? api<AccessGroup>(`/access/access-groups/${input.id}`, json('PATCH', input.body))
      : api<AccessGroup>('/access/access-groups', json('POST', input.body)),
    onSuccess: after,
  })
  const remove = useMutation({ mutationFn: (id: string) => api(`/access/access-groups/${id}`, json('DELETE')), onSuccess: after })

  if (groups.isLoading) return <Loading />

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p className="muted">Grant a set of roles to many people at once.</p>
        <p className="text-xs text-slate-500">These are groups of <strong>people</strong> — applications and rings are managed under Applications.</p>
      </div>
      {!editing && <button type="button" className="btn-primary" onClick={() => setEditing('new')}>New access group</button>}
    </div>
    {(groups.error || save.error || remove.error) && <ErrorNotice error={groups.error ?? save.error ?? remove.error} />}

    {editing && <GroupEditor
      group={editing === 'new' ? null : editing}
      roles={roles.data ?? []}
      busy={save.isPending}
      onCancel={() => setEditing(null)}
      onSave={(body) => save.mutate({ id: editing === 'new' ? undefined : editing.id, body })}
    />}

    <div className="space-y-2">
      {(groups.data ?? []).map((group) => <div key={group.id} className="surface flex flex-wrap items-center justify-between gap-3 p-4">
        <div className="min-w-0">
          <p className="font-medium text-slate-900">{group.name}</p>
          <p className="text-xs text-slate-500">{group.description || '—'} · {group.member_count} member{group.member_count === 1 ? '' : 's'}</p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {roleNames(roles.data ?? [], group.role_ids).map((name) => <Chip key={name} tone="info">{name}</Chip>)}
          </div>
        </div>
        <div className="flex gap-2">
          <button type="button" className="btn-secondary !py-1" onClick={() => setEditing(group)}>Edit</button>
          <button type="button" className="btn-danger !py-1" onClick={() => setRemoving(group)}>Delete</button>
        </div>
      </div>)}
      {(groups.data ?? []).length === 0 && <p className="surface p-8 text-center muted">No access groups yet.</p>}
    </div>

    <ConfirmDialog
      open={removing !== null}
      title={`Delete the ${removing?.name ?? ''} access group?`}
      confirmLabel="Delete group"
      busy={remove.isPending}
      onCancel={() => setRemoving(null)}
      onConfirm={() => removing && remove.mutate(removing.id)}
    >
      <p>Its {removing?.member_count ?? 0} member(s) lose the roles it granted. Roles assigned to them directly are unaffected.</p>
    </ConfirmDialog>
  </div>
}

function GroupEditor({ group, roles, busy, onCancel, onSave }: {
  group: AccessGroup | null
  roles: AccessRole[]
  busy: boolean
  onCancel: () => void
  onSave: (body: Record<string, unknown>) => void
}) {
  const [name, setName] = useState(group?.name ?? '')
  const [description, setDescription] = useState(group?.description ?? '')
  const [roleIds, setRoleIds] = useState<string[]>(group?.role_ids ?? [])

  return <section className="card space-y-4">
    <h3 className="font-semibold text-slate-900">{group ? `Edit ${group.name}` : 'New access group'}</h3>
    <div className="grid gap-4 md:grid-cols-2">
      <Field label="Name"><input value={name} onChange={(event) => setName(event.target.value)} placeholder="Platform on-call" /></Field>
      <Field label="Description"><input value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
    </div>
    <Field label="Roles granted to every member">
      <div className="flex flex-wrap gap-1.5">
        {roles.map((role) => <Toggle key={role.id} active={roleIds.includes(role.id)} onClick={() => setRoleIds(roleIds.includes(role.id) ? roleIds.filter((id) => id !== role.id) : [...roleIds, role.id])}>{role.name}</Toggle>)}
      </div>
    </Field>
    <div className="flex gap-3 border-t border-slate-200 pt-4">
      <button type="button" className="btn-primary" disabled={busy || !name.trim()} onClick={() => onSave({ name: name.trim(), description, role_ids: roleIds })}>{busy ? 'Saving…' : 'Save group'}</button>
      <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
    </div>
  </section>
}

// -- sessions ------------------------------------------------------------

function SessionsTab() {
  const invalidate = useInvalidate()
  const { format } = useDisplayTimezone()
  const sessions = useQuery({ queryKey: [...keys.sessions], queryFn: () => api<LoginSession[]>('/access/sessions') })
  const revoke = useMutation({ mutationFn: (id: string) => api(`/access/sessions/${id}/revoke`, json('POST')), onSuccess: () => invalidate(keys.sessions) })
  const sweep = useMutation({ mutationFn: () => api<{ revoked: number }>('/access/sessions/revoke-expired', json('POST')), onSuccess: () => invalidate(keys.sessions) })

  if (sessions.isLoading) return <Loading />
  const active = (sessions.data ?? []).filter((item) => !item.revoked_at)

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="muted">{active.length} active session{active.length === 1 ? '' : 's'} of the last 500 recorded.</p>
      <button type="button" className="btn-secondary" disabled={sweep.isPending} onClick={() => sweep.mutate()}>Revoke expired</button>
    </div>
    {(sessions.error || revoke.error) && <ErrorNotice error={sessions.error ?? revoke.error} />}

    <div className="surface divide-y divide-slate-200">
      {(sessions.data ?? []).map((item) => <div key={item.id} className="grid gap-3 p-4 md:grid-cols-[1fr_1fr_1.2fr_auto] md:items-center">
        <div><p className="font-medium text-slate-900">{item.username}</p><p className="text-xs text-slate-500">{item.auth_method} · {item.ip_address || 'unknown IP'}</p></div>
        <p className="text-sm text-slate-700">Seen {format(item.last_seen_at)}</p>
        <p className="truncate text-xs text-slate-500" title={item.user_agent ?? ''}>{item.user_agent || 'Unknown client'}</p>
        <button type="button" className="btn-secondary !py-1" disabled={!!item.revoked_at || revoke.isPending} onClick={() => revoke.mutate(item.id)}>{item.revoked_at ? 'Revoked' : 'Revoke'}</button>
      </div>)}
    </div>
  </div>
}

// -- policies ------------------------------------------------------------

const POLICY_FIELDS: { key: keyof AuthPolicies; label: string; kind: 'bool' | 'number'; hint?: string }[] = [
  { key: 'local_login_enabled', label: 'Allow username and password sign-in', kind: 'bool', hint: 'The break-glass administrator can always sign in locally.' },
  { key: 'password_min_length', label: 'Minimum password length', kind: 'number' },
  { key: 'password_require_upper', label: 'Require an uppercase letter', kind: 'bool' },
  { key: 'password_require_lower', label: 'Require a lowercase letter', kind: 'bool' },
  { key: 'password_require_number', label: 'Require a number', kind: 'bool' },
  { key: 'password_require_symbol', label: 'Require a symbol', kind: 'bool' },
  { key: 'lockout_attempts', label: 'Failed attempts before lockout', kind: 'number' },
  { key: 'lockout_minutes', label: 'Lockout duration (minutes)', kind: 'number' },
  { key: 'ip_lockout_enabled', label: 'Throttle repeated failures per IP address', kind: 'bool', hint: 'Catches one source trying many usernames, which no single account lockout would notice.' },
  { key: 'ip_lockout_attempts', label: 'Failed attempts per IP before throttling', kind: 'number' },
  { key: 'ip_lockout_window_seconds', label: 'IP failure window (seconds)', kind: 'number', hint: 'Only failures inside this window count, so an occasional typo never accumulates.' },
  { key: 'ip_lockout_seconds', label: 'IP throttle duration (seconds)', kind: 'number' },
  { key: 'session_idle_minutes', label: 'Session idle timeout (minutes)', kind: 'number' },
  { key: 'session_absolute_hours', label: 'Session absolute lifetime (hours)', kind: 'number' },
]

function PoliciesTab() {
  const invalidate = useInvalidate()
  const policies = useQuery({ queryKey: [...keys.policies], queryFn: () => api<AuthPolicies>('/access/policies') })
  const [draft, setDraft] = useState<Partial<AuthPolicies>>({})
  const save = useMutation({
    mutationFn: () => api<AuthPolicies>('/access/policies', json('PUT', { ...policies.data, ...draft })),
    onSuccess: () => { invalidate(keys.policies); setDraft({}) },
  })

  if (policies.isLoading || !policies.data) return <Loading />
  const values = { ...policies.data, ...draft }
  const dirty = Object.keys(draft).length > 0

  return <div className="max-w-2xl space-y-4">
    <p className="muted">How people sign in, and how long they stay signed in.</p>
    {(policies.error || save.error) && <ErrorNotice error={policies.error ?? save.error} />}
    <section className="card space-y-3">
      {POLICY_FIELDS.map((field) => <div key={field.key} className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 pb-3 last:border-0 last:pb-0">
        <div><p className="text-sm text-slate-800">{field.label}</p>{field.hint && <p className="text-xs text-slate-500">{field.hint}</p>}</div>
        {field.kind === 'bool'
          ? <input className="!w-auto" type="checkbox" aria-label={field.label} checked={!!values[field.key]} onChange={(event) => setDraft({ ...draft, [field.key]: event.target.checked })} />
          : <input className="!w-24" type="number" aria-label={field.label} value={Number(values[field.key])} onChange={(event) => setDraft({ ...draft, [field.key]: Number(event.target.value) })} />}
      </div>)}
      <div className="flex items-center gap-3 border-t border-slate-200 pt-4">
        <button type="button" className="btn-primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>{save.isPending ? 'Saving…' : 'Save policies'}</button>
        {!dirty && save.isSuccess && <span className="text-sm text-emerald-700">Saved.</span>}
      </div>
    </section>
  </div>
}

// -- sign-in and SSO -----------------------------------------------------

function SsoTab() {
  const invalidate = useInvalidate()
  const providers = useQuery({ queryKey: [...keys.providers], queryFn: () => api<IdentityProvider[]>('/access/identity-providers') })
  const [editing, setEditing] = useState<IdentityProvider | 'new' | null>(null)
  const [removing, setRemoving] = useState<IdentityProvider | null>(null)

  const after = () => { invalidate(keys.providers); setEditing(null); setRemoving(null) }
  const save = useMutation({
    mutationFn: (input: { id?: string; body: unknown }) => input.id
      ? api<IdentityProvider>(`/access/identity-providers/${input.id}`, json('PATCH', input.body))
      : api<IdentityProvider>('/access/identity-providers', json('POST', input.body)),
    onSuccess: after,
  })
  const remove = useMutation({ mutationFn: (id: string) => api(`/access/identity-providers/${id}`, json('DELETE')), onSuccess: after })

  if (providers.isLoading) return <Loading />

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="muted">Single sign-on providers offered on the sign-in page.</p>
      {!editing && <button type="button" className="btn-primary" onClick={() => setEditing('new')}>Add provider</button>}
    </div>
    {(providers.error || save.error || remove.error) && <ErrorNotice error={providers.error ?? save.error ?? remove.error} />}

    {editing && <ProviderEditor
      provider={editing === 'new' ? null : editing}
      busy={save.isPending}
      onCancel={() => setEditing(null)}
      onSave={(body) => save.mutate({ id: editing === 'new' ? undefined : editing.id, body })}
    />}

    <div className="space-y-2">
      {(providers.data ?? []).map((provider) => <div key={provider.id} className="surface flex flex-wrap items-center justify-between gap-3 p-4">
        <div>
          <p className="flex items-center gap-2 font-medium text-slate-900">{provider.name}<Chip tone={provider.enabled ? 'success' : 'neutral'}>{provider.enabled ? 'Enabled' : 'Disabled'}</Chip><Chip tone="neutral">{provider.type.toUpperCase()}</Chip></p>
          <p className="text-xs text-slate-500">{providerSubtitle(provider)}</p>
        </div>
        <div className="flex gap-2">
          <button type="button" className="btn-secondary !py-1" onClick={() => setEditing(provider)}>Edit</button>
          <button type="button" className="btn-danger !py-1" onClick={() => setRemoving(provider)}>Remove</button>
        </div>
      </div>)}
      {(providers.data ?? []).length === 0 && <p className="surface p-8 text-center muted">No single sign-on providers configured. People sign in with a username and password.</p>}
    </div>

    <ConfirmDialog
      open={removing !== null}
      title={`Remove ${removing?.name ?? ''}?`}
      confirmLabel="Remove provider"
      busy={remove.isPending}
      onCancel={() => setRemoving(null)}
      onConfirm={() => removing && remove.mutate(removing.id)}
    >
      <p>Anyone who signs in through it loses that route immediately. Accounts it provisioned are kept.</p>
    </ConfirmDialog>
  </div>
}

/**
 * Provider configuration is declared as data, per protocol, so adding a setting is a one-line
 * change here rather than another hand-written input.
 */
type ProviderField = { key: string; label: string; hint?: string; placeholder?: string; wide?: boolean }

const ENTRA_FIELDS: ProviderField[] = [
  { key: 'tenant_id', label: 'Directory (tenant) ID', hint: 'From the app registration Overview blade.' },
  { key: 'client_id', label: 'Application (client) ID' },
]
const OIDC_FIELDS: ProviderField[] = [
  { key: 'issuer', label: 'Issuer URL', placeholder: 'https://idp.example.com', hint: 'Exactly as the provider publishes it.' },
  { key: 'discovery_url', label: 'Discovery URL', placeholder: 'Defaults to <issuer>/.well-known/openid-configuration' },
  { key: 'client_id', label: 'Client ID' },
  { key: 'scopes', label: 'Scopes', placeholder: 'openid profile email' },
  { key: 'group_claim', label: 'Group claim', placeholder: 'groups', hint: 'ID token claim carrying group membership.' },
]
const SAML_FIELDS: ProviderField[] = [
  { key: 'entity_id', label: 'IdP Entity ID (Issuer)', placeholder: 'https://sts.windows.net/<tenant>/' },
  { key: 'sso_url', label: 'IdP SSO URL', placeholder: 'https://login.microsoftonline.com/<tenant>/saml2' },
  { key: 'certificate', label: 'IdP signing certificate', hint: 'PEM or bare base64. Used to verify every assertion.', wide: true },
  { key: 'email_attr', label: 'Email attribute', placeholder: 'Optional — common URIs are tried' },
  { key: 'name_attr', label: 'Name attribute', placeholder: 'Optional' },
  { key: 'group_attr', label: 'Group attribute', placeholder: 'Optional' },
]

const FIELDS_BY_TYPE: Record<ProviderType, ProviderField[]> = { entra: ENTRA_FIELDS, oidc: OIDC_FIELDS, saml: SAML_FIELDS }
const TYPE_LABELS: Record<ProviderType, string> = { entra: 'Microsoft Entra ID (OIDC)', oidc: 'OpenID Connect', saml: 'SAML 2.0' }

const origin = () => (typeof window === 'undefined' ? '' : window.location.origin)

/** One line summarising whether a provider is actually usable, not just present. */
function providerSubtitle(provider: IdentityProvider): string {
  const config = provider.config as Record<string, unknown>
  if (provider.type === 'saml') {
    return `${String(config.entity_id || 'no entity id')} · ${config.certificate ? 'certificate set' : 'no certificate'}`
  }
  const where = String(config.issuer || config.tenant_id || 'no issuer set')
  return `${where} · ${provider.has_client_secret ? 'secret stored' : 'no secret'}`
}

function ProviderEditor({ provider, busy, onCancel, onSave }: {
  provider: IdentityProvider | null
  busy: boolean
  onCancel: () => void
  onSave: (body: Record<string, unknown>) => void
}) {
  const roles = useRoles()
  const [type, setType] = useState<ProviderType>(provider?.type ?? 'entra')
  const [name, setName] = useState(provider?.name ?? 'Microsoft Entra ID')
  const [enabled, setEnabled] = useState(provider?.enabled ?? false)
  const [buttonLabel, setButtonLabel] = useState(provider?.button_label ?? 'Sign in with Microsoft')
  const [config, setConfig] = useState<Record<string, string>>(() => {
    const source = (provider?.config ?? {}) as Record<string, unknown>
    return Object.fromEntries(
      Object.entries(source)
        .filter(([key]) => key !== 'group_role_map' && key !== 'client_secret_encrypted')
        .map(([key, value]) => [key, String(value ?? '')]),
    )
  })
  const [secret, setSecret] = useState('')
  const [autoProvision, setAutoProvision] = useState(Boolean(provider?.config.auto_provision ?? false))
  const [defaultRole, setDefaultRole] = useState(String(provider?.config.default_role ?? 'noaccess'))
  const [mapping, setMapping] = useState<Record<string, string>>((provider?.config.group_role_map as Record<string, string>) ?? {})
  const [test, setTest] = useState<IdpTestResult | null>(null)

  const fields = FIELDS_BY_TYPE[type]
  const set = (key: string, value: string) => setConfig((current) => ({ ...current, [key]: value }))
  const body = () => ({
    id: provider?.id,
    name: name.trim(),
    type,
    enabled,
    button_label: buttonLabel,
    config: {
      ...Object.fromEntries(fields.map((field) => [field.key, (config[field.key] ?? '').trim()])),
      auto_provision: autoProvision,
      default_role: defaultRole,
      group_role_map: mapping,
    },
    client_secret: secret || null,
  })
  const check = useMutation({ mutationFn: () => api<IdpTestResult>('/access/identity-providers/test', json('POST', body())), onSuccess: setTest })

  return <section className="card space-y-4">
    <h3 className="font-semibold text-slate-900">{provider ? `Edit ${provider.name}` : 'New identity provider'}</h3>

    {!provider && <Field label="Protocol" hint="Entra ID and OpenID Connect share one flow; SAML is a separate one.">
      <select value={type} onChange={(event) => setType(event.target.value as ProviderType)}>
        {(Object.keys(TYPE_LABELS) as ProviderType[]).map((item) => <option key={item} value={item}>{TYPE_LABELS[item]}</option>)}
      </select>
    </Field>}

    <SsoSetupGuide type={type} providerId={provider?.id} />

    <div className="grid gap-4 md:grid-cols-2">
      <Field label="Display name"><input value={name} onChange={(event) => setName(event.target.value)} /></Field>
      <Field label="Sign-in button label"><input value={buttonLabel} onChange={(event) => setButtonLabel(event.target.value)} /></Field>
      {fields.map((field) => <Field key={field.key} label={field.label} hint={field.hint} wide={field.wide}>
        {field.key === 'certificate'
          ? <textarea rows={4} className="font-mono text-xs" value={config[field.key] ?? ''} placeholder={field.placeholder} onChange={(event) => set(field.key, event.target.value)} />
          : <input value={config[field.key] ?? ''} placeholder={field.placeholder} autoComplete="off" onChange={(event) => set(field.key, event.target.value)} />}
      </Field>)}
      {type !== 'saml' && <Field label="Client secret" hint={provider?.has_client_secret ? 'Stored encrypted — leave blank to keep it.' : 'Paste the secret Value, not the Secret ID.'} wide>
        <input type="password" value={secret} onChange={(event) => setSecret(event.target.value)} autoComplete="new-password" />
      </Field>}
      <Field label="Default role for new accounts" hint="Applied when a person signs in for the first time.">
        <select value={defaultRole} onChange={(event) => setDefaultRole(event.target.value)}>
          {(roles.data ?? []).map((role) => <option key={role.id} value={role.name}>{role.name}</option>)}
        </select>
      </Field>
    </div>

    <label className="flex items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
      <input className="!w-auto" type="checkbox" checked={autoProvision} onChange={(event) => setAutoProvision(event.target.checked)} />
      <span><span className="block text-sm font-medium text-slate-800">Create accounts on first sign-in</span><span className="text-xs text-slate-500">Without this, a person must already exist here before they can use single sign-on.</span></span>
    </label>

    <Field label="Map directory groups to roles" wide hint="Matched against the group claim or attribute. Anyone unmatched falls back to the default role above.">
      <GroupRoleMap value={mapping} roles={roles.data ?? []} onChange={setMapping} />
    </Field>

    <label className="flex items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
      <input className="!w-auto" type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
      <span><span className="block text-sm font-medium text-slate-800">Offer this provider on the sign-in page</span></span>
    </label>

    {test && <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
      <p className={`mb-2 text-sm font-semibold ${test.ok ? 'text-emerald-700' : 'text-rose-700'}`}>{test.ok ? '✓' : '✕'} {test.summary}</p>
      <ul className="space-y-1">
        {test.checks.map((item) => <li key={item.name} className="flex flex-wrap items-start gap-2 text-xs">
          <span className={item.ok ? 'text-emerald-600' : item.critical ? 'text-rose-600' : 'text-amber-600'}>{item.ok ? '✓' : item.critical ? '✕' : '!'}</span>
          <span className="font-medium text-slate-700">{item.name}:</span>
          <span className="text-slate-500">{item.detail}</span>
        </li>)}
      </ul>
    </div>}

    <div className="flex flex-wrap gap-3 border-t border-slate-200 pt-4">
      <button type="button" className="btn-primary" disabled={busy || !name.trim()} onClick={() => onSave(body())}>{busy ? 'Saving…' : 'Save provider'}</button>
      <button type="button" className="btn-secondary" disabled={check.isPending} onClick={() => check.mutate()}>{check.isPending ? 'Checking…' : 'Test configuration'}</button>
      <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
    </div>
  </section>
}

/** The values an administrator has to paste into the identity provider, per protocol. */
function SsoSetupGuide({ type, providerId }: { type: ProviderType; providerId?: string }) {
  const id = providerId ?? '<saved provider id>'
  const rows = type === 'saml'
    ? [
      { label: 'Identifier (Entity ID)', value: `${origin()}/api/auth/saml/metadata` },
      { label: 'Reply URL (ACS)', value: `${origin()}/api/auth/saml/${id}/acs` },
      { label: 'SP metadata', value: `${origin()}/api/auth/saml/${id}/metadata` },
    ]
    : [{ label: 'Redirect URI', value: `${origin()}/api/auth/oidc/${id}/callback` }]

  return <SetupGuide title={`${TYPE_LABELS[type]} setup`} defaultOpen={!providerId}>
    {!providerId && <Callout tone="warn" title="Save the provider first">
      These URLs contain the provider's id, which only exists once it has been saved. Save it, reopen it, then copy them.
    </Callout>}
    <Step n={1} title="Register the application">
      {type === 'saml'
        ? 'Create an enterprise application in your identity provider and choose SAML as the single sign-on method.'
        : 'Create an app registration for a web application in your identity provider.'}
    </Step>
    <Step n={2} title={type === 'saml' ? 'Set the identifier and reply URL' : 'Add the redirect URI'}>
      Copy these exactly — a trailing slash is enough to break sign-in.
      {rows.map((row) => <CopyRow key={row.label} label={row.label} value={row.value} />)}
    </Step>
    {type === 'saml'
      ? <Step n={3} title="Copy the signing certificate">Download the Base64 signing certificate and paste it below, with the Entity ID and SSO URL.</Step>
      : <Step n={3} title="Create a client secret">Paste the secret's <strong>Value</strong> below, not the Secret ID. It is encrypted before storage and never shown again.</Step>}
    <Step n={4} title="Decide what a new person gets">
      Auto-provisioned accounts receive the default role, which is <code className="font-mono">noaccess</code> unless you change it. Map groups to roles below to grant access automatically.
    </Step>
  </SetupGuide>
}

function GroupRoleMap({ value, roles, onChange }: { value: Record<string, string>; roles: AccessRole[]; onChange: (next: Record<string, string>) => void }) {
  const entries = Object.entries(value)
  const set = (index: number, key: string, role: string) => onChange(Object.fromEntries(entries.map((entry, position) => position === index ? [key, role] : entry)))
  return <div className="space-y-2">
    {entries.length === 0 && <p className="text-xs text-slate-500">No mappings. Everyone falls back to the default role.</p>}
    {entries.map(([key, role], index) => <div key={index} className="flex flex-wrap items-center gap-2">
      <input className="!w-64" value={key} placeholder="Group object id or name" onChange={(event) => set(index, event.target.value, role)} />
      <span className="text-slate-400" aria-hidden="true">→</span>
      <select className="!w-auto" value={role} onChange={(event) => set(index, key, event.target.value)}>
        {roles.map((item) => <option key={item.id} value={item.name}>{item.name}</option>)}
      </select>
      <button type="button" className="link text-xs" onClick={() => { const next = { ...value }; delete next[key]; onChange(next) }}>Remove</button>
    </div>)}
    <button type="button" className="link text-xs" onClick={() => onChange({ ...value, '': roles[0]?.name ?? 'viewer' })}>+ Add mapping</button>
  </div>
}

// -- IP access -----------------------------------------------------------

const MODE_CARDS: { key: IpAllowlistMode; label: string; blurb: string }[] = [
  { key: 'disabled', label: 'Off', blurb: 'No filtering. Any address on the internet may reach the app.' },
  { key: 'audit', label: 'Audit', blurb: 'Record what would be refused, refuse nothing. The safe first step.' },
  { key: 'enforce', label: 'Enforce', blurb: 'Refuse anything that is not on the list below.' },
]

const SCOPE_CARDS: { key: IpAllowlistScope; label: string; blurb: string }[] = [
  { key: 'auth_only', label: 'Sign-in only', blurb: 'Login, password change and SSO. Everything else stays reachable.' },
  { key: 'all', label: 'Entire application', blurb: 'Every request, including the web interface itself.' },
]

/** "in 12 minutes", for the commit-confirm countdown. */
function untilText(iso: string): string {
  const seconds = Math.round((new Date(iso).getTime() - Date.now()) / 1000)
  if (seconds <= 0) return 'any moment now'
  const minutes = Math.floor(seconds / 60)
  return minutes >= 1 ? `in ${minutes} minute${minutes === 1 ? '' : 's'}` : `in ${seconds} seconds`
}

function FirewallTab() {
  const invalidate = useInvalidate()
  const { format } = useDisplayTimezone()
  const state = useQuery({
    queryKey: [...keys.ipAccess],
    queryFn: () => api<IpAccessState>('/access/ip-rules'),
    // The auto-revert countdown has to stay honest without a reload.
    refetchInterval: 30_000,
  })
  const blocks = useQuery({ queryKey: [...keys.ipBlocks], queryFn: () => api<IpBlockEvent[]>('/access/ip-blocks') })
  const [draft, setDraft] = useState<{ mode: IpAllowlistMode; scope: IpAllowlistScope } | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [adding, setAdding] = useState<{ cidr: string; label: string } | null>(null)
  const [removing, setRemoving] = useState<IpRule | null>(null)
  const [showBlocks, setShowBlocks] = useState(false)

  const after = () => { invalidate(keys.ipAccess); invalidate(keys.ipBlocks); setDraft(null); setConfirming(false); setAdding(null); setRemoving(null) }
  const savePolicy = useMutation({
    mutationFn: (body: { mode: IpAllowlistMode; scope: IpAllowlistScope; confirm_minutes: number }) => api<IpAccessState>('/access/ip-policy', json('PUT', body)),
    onSuccess: after,
  })
  const confirmPolicy = useMutation({ mutationFn: () => api<IpAccessState>('/access/ip-policy/confirm', json('POST')), onSuccess: after })
  const addRule = useMutation({ mutationFn: (body: { cidr: string; label: string }) => api<IpRule>('/access/ip-rules', json('POST', body)), onSuccess: after })
  const toggleRule = useMutation({ mutationFn: (input: { id: string; enabled: boolean }) => api<IpRule>(`/access/ip-rules/${input.id}`, json('PATCH', { enabled: input.enabled })), onSuccess: after })
  const removeRule = useMutation({ mutationFn: (id: string) => api(`/access/ip-rules/${id}`, json('DELETE')), onSuccess: after })

  if (state.isLoading || !state.data) return <Loading />
  const data = state.data
  const values = draft ?? { mode: data.mode, scope: data.scope }
  const dirty = values.mode !== data.mode || values.scope !== data.scope
  const error = state.error ?? savePolicy.error ?? confirmPolicy.error ?? addRule.error ?? toggleRule.error ?? removeRule.error
  const enforcing = data.mode === 'enforce'
  const auditing = data.mode === 'audit'
  const wouldBlock = blocks.data?.filter((item) => item.audit_only).reduce((total, item) => total + item.hit_count, 0) ?? 0

  const applyPolicy = () => {
    if (values.mode === 'enforce' && data.mode !== 'enforce') { setConfirming(true); return }
    savePolicy.mutate({ ...values, confirm_minutes: 0 })
  }

  const banner = data.kill_switch
    ? { tone: 'border-amber-200 bg-amber-50 text-amber-900', text: 'A kill switch in the environment (IP_ALLOWLIST_DISABLED) is overriding every setting on this page. Nothing is being filtered.' }
    : enforcing
      ? { tone: 'border-emerald-200 bg-emerald-50 text-emerald-900', text: `Enforcing. ${data.enabled_rule_count} allowed range${data.enabled_rule_count === 1 ? '' : 's'} · scope: ${data.scope === 'all' ? 'entire application' : 'sign-in only'}.` }
      : auditing
        ? { tone: 'border-amber-200 bg-amber-50 text-amber-900', text: `Audit only. Nothing is being refused${wouldBlock ? `; ${wouldBlock} request${wouldBlock === 1 ? '' : 's'} would have been` : ''}.` }
        : { tone: 'border-slate-200 bg-slate-50 text-slate-700', text: 'IP access control is off. Anyone on the internet can reach the sign-in page and attempt to authenticate.' }

  return <div className="space-y-4">
    <p className="muted">Restrict which source addresses may reach this application. The surest defence against password guessing is never receiving the attempt.</p>
    {error && <ErrorNotice error={error} />}

    <div className={`flex flex-wrap items-center justify-between gap-3 rounded-lg border px-4 py-3 text-sm ${banner.tone}`}>
      <span>{banner.text}</span>
      <span className="flex items-center gap-2">
        <span className="text-xs opacity-80">Your address</span>
        <code className="font-mono text-xs">{data.your_ip ?? 'unknown'}</code>
        {data.your_ip_allowed
          ? <Chip tone="success">on the list</Chip>
          : <Chip tone="danger">not on the list</Chip>}
      </span>
    </div>

    {/* Commit-confirm: the recovery path that needs no Azure access at all. */}
    {data.confirm_by && <Callout tone="warn" title="Confirm you can still reach the app">
      Enforcement reverts to audit {untilText(data.confirm_by)} unless you confirm. You are reading this page, so access works — confirming now keeps it on.
      <div className="mt-2">
        <button type="button" className="btn-primary !py-1" disabled={confirmPolicy.isPending} onClick={() => confirmPolicy.mutate()}>Keep enforcement on</button>
      </div>
    </Callout>}

    <section className="card space-y-4">
      <div>
        <p className="text-sm font-semibold text-slate-800">Mode</p>
        <div className="mt-2 grid gap-2 md:grid-cols-3">
          {MODE_CARDS.map((card) => <label key={card.key} className={`cursor-pointer rounded-lg border p-3 transition ${values.mode === card.key ? 'border-blue-500 bg-blue-50' : 'border-slate-200 hover:border-slate-300'}`}>
            <span className="flex items-center gap-2">
              <input type="radio" className="!w-auto" name="ip-mode" checked={values.mode === card.key} onChange={() => setDraft({ ...values, mode: card.key })} />
              <span className="text-sm font-medium text-slate-800">{card.label}</span>
              {card.key === 'audit' && <Chip tone="info">recommended first</Chip>}
            </span>
            <span className="mt-1 block text-xs text-slate-600">{card.blurb}</span>
          </label>)}
        </div>
      </div>

      <div>
        <p className="text-sm font-semibold text-slate-800">Scope</p>
        <div className="mt-2 grid gap-2 md:grid-cols-2">
          {SCOPE_CARDS.map((card) => <label key={card.key} className={`cursor-pointer rounded-lg border p-3 transition ${values.mode === 'disabled' ? 'opacity-50' : ''} ${values.scope === card.key ? 'border-blue-500 bg-blue-50' : 'border-slate-200 hover:border-slate-300'}`}>
            <span className="flex items-center gap-2">
              <input type="radio" className="!w-auto" name="ip-scope" disabled={values.mode === 'disabled'} checked={values.scope === card.key} onChange={() => setDraft({ ...values, scope: card.key })} />
              <span className="text-sm font-medium text-slate-800">{card.label}</span>
            </span>
            <span className="mt-1 block text-xs text-slate-600">{card.blurb}</span>
          </label>)}
        </div>
      </div>

      <div className="flex items-center gap-3 border-t border-slate-200 pt-4">
        <button type="button" className="btn-primary" disabled={!dirty || savePolicy.isPending} onClick={applyPolicy}>{savePolicy.isPending ? 'Saving…' : 'Save'}</button>
        {dirty && <button type="button" className="btn-secondary" onClick={() => setDraft(null)}>Cancel</button>}
      </div>
    </section>

    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm font-semibold text-slate-800">Allowed addresses</p>
        <div className="flex gap-2">
          {data.your_ip && !data.your_ip_allowed && <button type="button" className="btn-secondary" onClick={() => setAdding({ cidr: data.your_ip ?? '', label: 'My current address' })}>Add my current IP</button>}
          {!adding && <button type="button" className="btn-primary" onClick={() => setAdding({ cidr: '', label: '' })}>Add range</button>}
        </div>
      </div>

      {adding && <div className="card grid gap-3 md:grid-cols-[1fr_1.5fr_auto] md:items-end">
        <Field label="Address or range" hint="A bare address is stored as a single host, e.g. 203.0.113.4/32.">
          <input value={adding.cidr} autoFocus onChange={(event) => setAdding({ ...adding, cidr: event.target.value })} placeholder="203.0.113.0/24" />
        </Field>
        <Field label="Label" hint="Who or where this is, so it can be retired confidently later.">
          <input value={adding.label} onChange={(event) => setAdding({ ...adding, label: event.target.value })} placeholder="Head office VPN" />
        </Field>
        <div className="flex gap-2 pb-1">
          <button type="button" className="btn-primary" disabled={!adding.cidr.trim() || addRule.isPending} onClick={() => addRule.mutate({ cidr: adding.cidr.trim(), label: adding.label.trim() })}>Add</button>
          <button type="button" className="btn-secondary" onClick={() => setAdding(null)}>Cancel</button>
        </div>
      </div>}

      {data.rules.length === 0
        ? <div className="surface p-6 text-center">
            <p className="text-sm font-medium text-slate-800">No addresses allowed yet</p>
            <p className="mt-1 text-xs text-slate-500">Add your own address first — enforcement cannot be turned on from an address that is not on the list.</p>
          </div>
        : <div className="surface divide-y divide-slate-200">
            {data.rules.map((rule) => {
              const mine = rule.id === data.your_ip_rule_id
              return <div key={rule.id} className={`grid gap-3 p-4 md:grid-cols-[1fr_1.2fr_1fr_auto] md:items-center ${mine ? 'border-l-4 border-l-emerald-500' : ''}`}>
                <p className="flex items-center gap-2 font-mono text-sm text-slate-900">{rule.cidr}{mine && <Chip tone="success">you</Chip>}</p>
                <p className="truncate text-sm text-slate-600">{rule.label || <span className="text-slate-400">No label</span>}</p>
                <p className="text-xs text-slate-500">{rule.last_seen_at ? `Last used ${format(rule.last_seen_at)}` : 'Never used'}</p>
                <div className="flex justify-end gap-2">
                  <button type="button" className="btn-secondary !py-1" disabled={toggleRule.isPending} onClick={() => toggleRule.mutate({ id: rule.id, enabled: !rule.enabled })}>{rule.enabled ? 'Disable' : 'Enable'}</button>
                  <button type="button" className="btn-secondary !py-1" aria-label={`Delete ${rule.cidr}`} onClick={() => setRemoving(rule)}><Trash2 size={14} /></button>
                </div>
              </div>
            })}
          </div>}

      {data.bootstrap_ranges.length > 0 && <Callout tone="neutral" title="Always allowed by the environment">
        {data.bootstrap_ranges.join(', ')} — set through IP_ALLOWLIST_BOOTSTRAP. These cannot be edited here, which is what makes them a way back in.
      </Callout>}
    </section>

    <section className="surface">
      <button type="button" className="flex w-full items-center gap-2 px-4 py-3 text-left text-sm font-semibold text-slate-800" aria-expanded={showBlocks || auditing} onClick={() => setShowBlocks(!showBlocks)}>
        Recent refusals
        <span className="text-xs font-normal text-slate-500">{blocks.data?.length ?? 0} source{(blocks.data?.length ?? 0) === 1 ? '' : 's'}</span>
      </button>
      {(showBlocks || auditing) && <div className="divide-y divide-slate-200 border-t border-slate-200">
        {(blocks.data ?? []).length === 0 && <p className="p-4 text-sm text-slate-500">Nothing has been refused.</p>}
        {(blocks.data ?? []).map((item) => <div key={item.id} className="grid gap-3 p-4 md:grid-cols-[1fr_1fr_1fr_auto] md:items-center">
          <p className="flex items-center gap-2 font-mono text-sm text-slate-900">{item.ip}{item.audit_only && <Chip tone="warn">audit</Chip>}</p>
          <p className="text-sm text-slate-600">{item.hit_count} request{item.hit_count === 1 ? '' : 's'} · {item.path_class}</p>
          <p className="text-xs text-slate-500">Last {format(item.last_seen_at)}</p>
          <button type="button" className="btn-secondary !py-1" onClick={() => setAdding({ cidr: item.ip, label: '' })}>Allow this address</button>
        </div>)}
      </div>}
    </section>

    <ConfirmDialog
      open={confirming}
      title="Turn on enforcement?"
      tone={data.your_ip_allowed ? 'primary' : 'danger'}
      confirmLabel="Enforce with a 15 minute safety timer"
      confirmDisabled={!data.your_ip_allowed || savePolicy.isPending}
      busy={savePolicy.isPending}
      onCancel={() => setConfirming(false)}
      onConfirm={() => savePolicy.mutate({ ...values, confirm_minutes: 15 })}
    >
      {data.your_ip_allowed
        ? <>Requests from addresses other than the {data.enabled_rule_count} allowed range{data.enabled_rule_count === 1 ? '' : 's'} will be refused. Your address <code className="font-mono">{data.your_ip}</code> is on the list.
            <p className="mt-2">Enforcement reverts to audit automatically after 15 minutes unless you confirm on this page, so a mistake cannot lock you out.</p></>
        : <>Your address <code className="font-mono">{data.your_ip ?? 'unknown'}</code> is not on the list, so enforcing would lock you out immediately. Add it first.</>}
    </ConfirmDialog>

    <ConfirmDialog
      open={!!removing}
      title={`Delete ${removing?.cidr ?? ''}?`}
      confirmLabel="Delete"
      busy={removeRule.isPending}
      onCancel={() => setRemoving(null)}
      onConfirm={() => removing && removeRule.mutate(removing.id)}
    >
      Requests from this range will no longer be allowed while enforcement is on.
    </ConfirmDialog>
  </div>
}

// -- page ----------------------------------------------------------------
const TAB_KEYS = TABS.map((item) => item.key)

export function AccessControlPage() {
  const { user } = useAuth()
  // The tab lives in the URL so a section can be linked, bookmarked and shared — "look at the
  // roles tab" should be a URL, not an instruction.
  const [params, setParams] = useSearchParams()
  const requested = params.get('tab') as Tab | null
  const tab: Tab = requested && TAB_KEYS.includes(requested) ? requested : 'users'
  const select = (next: Tab) => setParams(next === 'users' ? {} : { tab: next }, { replace: true })

  return <>
    <PageHeader title="Access control" description="Who can sign in, what they can do, and where they are signed in from." />
    {user?.is_break_glass && <div className="mb-4"><Callout tone="info" title="You are signed in as the break-glass administrator">
      This account cannot be disabled or deleted, so it is always a way back in. Day-to-day work is better done from a named account.
    </Callout></div>}

    <div className="mb-5 flex flex-wrap gap-1 border-b border-slate-200" role="tablist" aria-label="Access control sections">
      {TABS.map((item) => {
        const Icon = item.icon
        const active = tab === item.key
        return <button
          key={item.key}
          type="button"
          role="tab"
          aria-selected={active}
          onClick={() => select(item.key)}
          className={`-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-azure ${active ? 'border-blue-600 text-blue-700' : 'border-transparent text-slate-500 hover:text-slate-800'}`}
        ><Icon size={15} aria-hidden="true" />{item.label}</button>
      })}
    </div>

    {tab === 'users' && <UsersTab />}
    {tab === 'roles' && <RolesTab />}
    {tab === 'groups' && <GroupsTab />}
    {tab === 'sessions' && <SessionsTab />}
    {tab === 'policies' && <PoliciesTab />}
    {tab === 'firewall' && <FirewallTab />}
    {tab === 'sso' && <SsoTab />}
  </>
}
