import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, json } from '../api'
import { connectionOptionLabel, subtreeIds, useConnections } from '../lib/queries'
import { Drawer } from './Overlay'
import { GroupPicker } from './GroupPicker'
import { ErrorNotice, Field } from './Ui'
import type { Group, GroupNode } from '../types'

function invalidate(client: ReturnType<typeof useQueryClient>) {
  void client.invalidateQueries({ queryKey: ['groups'] })
  void client.invalidateQueries({ queryKey: ['group'] })
  void client.invalidateQueries({ queryKey: ['dashboard'] })
}

type EditorProps = {
  open: boolean
  onClose: () => void
  /** Provide to edit an existing group; omit to create a new one. */
  group?: Group | null
  /** Parent for a new group — null creates a top-level application. */
  parentId?: string | null
  parentName?: string | null
  onCreated?: (group: Group) => void
}

/** Create or rename an application/ring, including its description, tenant override and enabled state. */
export function GroupEditorDrawer({ open, onClose, group, parentId = null, parentName, onCreated }: EditorProps) {
  const client = useQueryClient()
  const connections = useConnections()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [connectionId, setConnectionId] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [neverStop, setNeverStop] = useState(false)

  useEffect(() => {
    if (!open) return
    setName(group?.name ?? '')
    setDescription(group?.description ?? '')
    setConnectionId(group?.azure_connection_id ?? '')
    setEnabled(group?.enabled ?? true)
    setNeverStop(group?.never_stop ?? false)
  }, [open, group])

  const save = useMutation({
    mutationFn: async () => {
      const body = { name: name.trim(), description, azure_connection_id: connectionId || null, enabled, never_stop: neverStop }
      if (group) return api<Group>(`/groups/${group.id}`, json('PATCH', body))
      return api<Group>('/groups', json('POST', { ...body, parent_id: parentId }))
    },
    onSuccess: (created) => { invalidate(client); onCreated?.(created); onClose() },
  })

  const isRing = group ? group.depth > 0 : parentId !== null
  const title = group ? `Edit ${group.kind}` : isRing ? 'New ring' : 'New application'

  return <Drawer
    open={open}
    onClose={onClose}
    title={title}
    description={group ? group.name_path : parentName ? `Created inside ${parentName}` : 'Applications sit at the top of the hierarchy.'}
    footer={<>
      <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
      <button type="button" className="btn-primary" disabled={!name.trim() || save.isPending} onClick={() => save.mutate()}>{save.isPending ? 'Saving…' : group ? 'Save changes' : 'Create'}</button>
    </>}
  >
    <div className="space-y-4">
      {save.error && <ErrorNotice error={save.error} />}
      <Field label="Name"><input value={name} onChange={(event) => setName(event.target.value)} placeholder={isRing ? 'Ring 1' : 'Payments platform'} required /></Field>
      <Field label="Description"><textarea rows={3} value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
      <Field label="Azure tenant" hint="Leave blank to inherit the nearest ancestor tenant or the default connection.">
        <select value={connectionId} onChange={(event) => setConnectionId(event.target.value)}>
          <option value="">Inherit from parent / default</option>
          {connections.data?.map((item) => <option key={item.id} value={item.id}>{connectionOptionLabel(item)}</option>)}
        </select>
      </Field>
      <label className="flex items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
        <input className="!w-auto" type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
        <span><span className="block text-sm font-medium text-slate-800">Enabled</span><span className="text-xs text-slate-500">Disabling stops every schedule beneath this node.</span></span>
      </label>
      <label className="flex items-center gap-3 rounded-lg border border-sky-200 bg-sky-50 p-3">
        <input className="!w-auto" type="checkbox" checked={neverStop} onChange={(event) => setNeverStop(event.target.checked)} />
        <span><span className="block text-sm font-medium text-slate-800">Never stop</span><span className="text-xs text-slate-600">Stop waves skip every machine beneath this node. Starts are unaffected.</span></span>
      </label>
    </div>
  </Drawer>
}

/** Move a group under a different parent. The group's own subtree is excluded from the picker. */
export function MoveGroupDrawer({ open, onClose, group, tree }: { open: boolean; onClose: () => void; group: GroupNode | null; tree: GroupNode[] }) {
  const client = useQueryClient()
  const [parent, setParent] = useState<string | null>(null)
  useEffect(() => { if (open) setParent(group?.parent_id ?? null) }, [open, group])

  const move = useMutation({
    mutationFn: () => api<Group>(`/groups/${group?.id}/move`, json('POST', { parent_id: parent })),
    onSuccess: () => { invalidate(client); onClose() },
  })

  if (!group) return null
  const excluded = subtreeIds(group)
  return <Drawer
    open={open}
    onClose={onClose}
    title={`Move ${group.name}`}
    description="Pick a new parent. The group and everything beneath it move together."
    footer={<>
      <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
      <button type="button" className="btn-primary" disabled={move.isPending || parent === group.parent_id} onClick={() => move.mutate()}>{move.isPending ? 'Moving…' : 'Move group'}</button>
    </>}
  >
    <div className="space-y-4">
      {move.error && <ErrorNotice error={move.error} />}
      <p className="muted">Current location: <span className="font-medium text-slate-800">{group.name_path}</span></p>
      <GroupPicker nodes={tree} value={parent} onChange={setParent} excluded={excluded} allowRoot label="New parent" />
    </div>
  </Drawer>
}
