import { useMemo, useState, type ReactElement } from 'react'
import { ChevronDown, ChevronRight, FolderTree, Layers } from 'lucide-react'
import type { GroupNode } from '../types'

type Props = {
  nodes: GroupNode[]
  value: string | null
  onChange: (id: string | null) => void
  /** Group ids that cannot be selected (for example a group's own subtree when moving it). */
  excluded?: Set<string>
  /** Allow choosing "no parent" — used when moving a ring up to become an application. */
  allowRoot?: boolean
  rootLabel?: string
  label?: string
}

/** Accessible tree select over the application/ring hierarchy. */
export function GroupPicker({ nodes, value, onChange, excluded, allowRoot, rootLabel = 'No parent (top-level application)', label = 'Group' }: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(nodes.map((item) => item.id)))
  const ordered = useMemo(() => [...nodes].sort((a, b) => a.sequence - b.sequence || a.name.localeCompare(b.name)), [nodes])

  const row = (node: GroupNode, depth: number): ReactElement => {
    const children = [...(node.children ?? [])].sort((a, b) => a.sequence - b.sequence || a.name.localeCompare(b.name))
    const isOpen = expanded.has(node.id)
    const blocked = excluded?.has(node.id) ?? false
    return <li key={node.id} role="treeitem" aria-selected={value === node.id} aria-expanded={children.length ? isOpen : undefined}>
      <div className="flex items-center gap-1" style={{ paddingLeft: `${depth * 14}px` }}>
        {children.length ? <button type="button" aria-label={isOpen ? `Collapse ${node.name}` : `Expand ${node.name}`} className="rounded p-0.5 text-slate-500 hover:bg-slate-100" onClick={() => setExpanded((current) => { const next = new Set(current); if (next.has(node.id)) next.delete(node.id); else next.add(node.id); return next })}>{isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</button> : <span className="w-[22px]" aria-hidden="true" />}
        <button type="button" disabled={blocked} onClick={() => onChange(node.id)} className={`flex min-w-0 flex-1 items-center gap-2 rounded px-2 py-1 text-left text-sm transition ${blocked ? 'cursor-not-allowed text-slate-400' : value === node.id ? 'bg-blue-100 font-semibold text-blue-900' : 'text-slate-700 hover:bg-slate-100'}`}>
          {node.depth === 0 ? <Layers size={14} className="shrink-0 text-blue-600" aria-hidden="true" /> : <FolderTree size={14} className="shrink-0 text-slate-400" aria-hidden="true" />}
          <span className="truncate">{node.name}</span>
          <span className="ml-auto shrink-0 text-xs text-slate-500">{node.subtree_vm_count} VM{node.subtree_vm_count === 1 ? '' : 's'}</span>
        </button>
      </div>
      {children.length > 0 && isOpen && <ul role="group">{children.map((child) => row(child, depth + 1))}</ul>}
    </li>
  }

  return <div className="max-h-72 overflow-y-auto rounded-lg border border-slate-300 bg-white p-1">
    <ul role="tree" aria-label={label}>
      {allowRoot && <li role="treeitem" aria-selected={value === null}>
        <button type="button" onClick={() => onChange(null)} className={`w-full rounded px-2 py-1 text-left text-sm transition ${value === null ? 'bg-blue-100 font-semibold text-blue-900' : 'text-slate-700 hover:bg-slate-100'}`}>{rootLabel}</button>
      </li>}
      {ordered.length ? ordered.map((node) => row(node, 0)) : <li className="px-3 py-4 text-sm text-slate-500">No applications exist yet.</li>}
    </ul>
  </div>
}
