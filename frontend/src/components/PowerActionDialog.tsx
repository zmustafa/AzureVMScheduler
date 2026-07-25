import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'

import { api, json } from '../api'
import { actionMeta, STOP_MODE_HELP, STOP_MODE_LABEL } from '../lib/actions'
import { useGeneralSettings } from '../lib/queries'
import { ConfirmDialog } from './Overlay'
import { ErrorNotice, Field } from './Ui'
import type { ScheduleAction, ScheduleRun, StopMode, VirtualMachine } from '../types'

/**
 * On-demand start/stop for hand-picked machines.
 *
 * Stopping is the only action in the product that can cause an outage on machines someone is using,
 * so it asks for the machine count to be typed back. That is deliberately more friction than a
 * start, which is merely expensive.
 */
export function PowerActionDialog({ open, action, vms, onClose }: {
  open: boolean
  action: ScheduleAction
  vms: VirtualMachine[]
  onClose: () => void
}) {
  const client = useQueryClient()
  const navigate = useNavigate()
  const settings = useGeneralSettings()
  const [stopMode, setStopMode] = useState<StopMode>('deallocate')
  const [typed, setTyped] = useState('')

  useEffect(() => { if (open) { setTyped(''); setStopMode('deallocate') } }, [open])

  const protectedVms = vms.filter((vm) => vm.stop_protected)
  const eligible = action === 'stop' ? vms.filter((vm) => !vm.stop_protected) : vms
  const meta = actionMeta(action)
  const isReal = action === 'stop' ? settings.data?.real_azure_stops_enabled : settings.data?.real_azure_starts_enabled
  const confirmed = action === 'start' || typed.trim() === String(eligible.length)

  const run = useMutation({
    mutationFn: () => api<ScheduleRun>('/vms/power-action', json('POST', {
      vm_ids: eligible.map((vm) => vm.id),
      action,
      stop_mode: stopMode,
      ...(action === 'stop' ? { confirm_count: eligible.length } : {}),
    })),
    onSuccess: (created) => {
      void client.invalidateQueries({ queryKey: ['runs'] })
      void client.invalidateQueries({ queryKey: ['vms'] })
      onClose()
      navigate(`/runs/${created.id}`)
    },
  })

  const tenants = new Set(eligible.map((vm) => vm.effective_connection_name ?? 'default'))

  return <ConfirmDialog
    open={open}
    title={`${meta.label} ${eligible.length} virtual machine${eligible.length === 1 ? '' : 's'} now`}
    tone={action === 'stop' ? 'danger' : 'primary'}
    confirmLabel={`${meta.label} ${eligible.length}`}
    busy={run.isPending}
    confirmDisabled={!confirmed || eligible.length === 0}
    onCancel={onClose}
    onConfirm={() => confirmed && eligible.length > 0 && run.mutate()}
  >
    {run.error && <div className="mb-3"><ErrorNotice error={run.error} /></div>}

    <p>
      This runs immediately, outside any schedule, against tenant{tenants.size === 1 ? '' : 's'} <strong>{[...tenants].join(', ')}</strong>.
    </p>
    <p className="mt-2">
      Mode: <strong>{isReal ? 'real Azure calls' : 'mock — nothing in Azure will change'}</strong>.
    </p>

    {protectedVms.length > 0 && <p className="mt-2 rounded-lg border border-sky-200 bg-sky-50 p-2 text-sm text-sky-900">
      {protectedVms.length} of the {vms.length} selected machine{vms.length === 1 ? '' : 's'} {protectedVms.length === 1 ? 'is' : 'are'} marked <strong>never stop</strong> and will be left running.
    </p>}

    {eligible.length === 0 && <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-2 text-sm text-amber-900">
      Every selected machine is protected from stopping. Nothing would happen.
    </p>}

    {action === 'stop' && eligible.length > 0 && <div className="mt-3 space-y-3">
      <Field label="Stop mode" hint={STOP_MODE_HELP[stopMode]}>
        <select value={stopMode} onChange={(event) => setStopMode(event.target.value as StopMode)}>
          <option value="deallocate">{STOP_MODE_LABEL.deallocate}</option>
          <option value="power_off">{STOP_MODE_LABEL.power_off}</option>
        </select>
      </Field>
      <Field label={`Type ${eligible.length} to confirm`} hint="Stopping a machine someone is using causes an outage.">
        <input value={typed} onChange={(event) => setTyped(event.target.value)} inputMode="numeric" placeholder={String(eligible.length)} />
      </Field>
    </div>}
  </ConfirmDialog>
}
