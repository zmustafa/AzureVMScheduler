import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

/** Right-side drawer used for create/edit flows. Escape closes, focus moves inside on open. */
export function Drawer({ open, title, description, onClose, footer, children, width = 'max-w-xl' }: { open: boolean; title: string; description?: string; onClose: () => void; footer?: ReactNode; children: ReactNode; width?: string }) {
  const panel = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    const focusable = panel.current?.querySelector<HTMLElement>('input, select, textarea, button, [href]')
    focusable?.focus()
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])
  if (!open) return null
  return createPortal(
    <div className="fixed inset-0 z-50 flex justify-end">
      <button type="button" aria-label="Close drawer" className="absolute inset-0 bg-slate-900/40" onClick={onClose} />
      <div ref={panel} role="dialog" aria-modal="true" aria-label={title} className={`relative flex h-full w-full ${width} flex-col border-l border-slate-200 bg-white shadow-2xl`}>
        <header className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
          <div><h2 className="text-lg font-semibold text-slate-900">{title}</h2>{description && <p className="mt-0.5 muted">{description}</p>}</div>
          <button type="button" className="btn-secondary !px-2 !py-1" onClick={onClose} aria-label="Close"><X size={16} /></button>
        </header>
        <div className="flex-1 overflow-y-auto px-5 py-5">{children}</div>
        {footer && <footer className="flex justify-end gap-3 border-t border-slate-200 bg-slate-50 px-5 py-3">{footer}</footer>}
      </div>
    </div>,
    document.body,
  )
}

/** Modal used only for destructive or irreversible confirmations. */
export function ConfirmDialog({ open, title, tone = 'danger', confirmLabel = 'Confirm', confirmDisabled, busy, onCancel, onConfirm, children }: { open: boolean; title: string; tone?: 'danger' | 'primary'; confirmLabel?: string; confirmDisabled?: boolean; busy?: boolean; onCancel: () => void; onConfirm: () => void; children: ReactNode }) {
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onCancel() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onCancel])
  if (!open) return null
  return createPortal(
    <div className="fixed inset-0 z-50 grid place-items-center p-4">
      <button type="button" aria-label="Cancel" className="absolute inset-0 bg-slate-900/40" onClick={onCancel} />
      <div role="alertdialog" aria-modal="true" aria-label={title} className="relative w-full max-w-md rounded-xl border border-slate-200 bg-white p-5 shadow-2xl">
        <h2 className="text-lg font-semibold text-slate-900">{title}</h2>
        <div className="mt-2 text-sm text-slate-700">{children}</div>
        <div className="mt-5 flex justify-end gap-3">
          <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
          <button type="button" className={tone === 'danger' ? 'btn-danger' : 'btn-primary'} disabled={busy || confirmDisabled} onClick={onConfirm}>{busy ? 'Working…' : confirmLabel}</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
