import { Play, Square, type LucideIcon } from 'lucide-react'

import type { RingOrder, ScheduleAction, StopMode } from '../types'

/**
 * One vocabulary for start vs stop, used by every surface that shows a wave.
 *
 * The two colours are deliberately far apart on the wheel: mistaking a stop wave for a start wave
 * is the most expensive misread in the product, so they must never be confusable at a glance.
 */
type ActionMeta = {
  label: string
  verb: string
  /** Past tense, for finished work: "started 12 VMs". */
  past: string
  icon: LucideIcon
  /** Chip/badge classes. */
  chip: string
  /** Solid colour for timeline blocks and legends. */
  bar: string
  /** Foreground-only, for icons sitting on a white row. */
  fg: string
}

export const ACTION_META: Record<ScheduleAction, ActionMeta> = {
  start: {
    label: 'Start',
    verb: 'start',
    past: 'started',
    icon: Play,
    chip: 'border-emerald-200 bg-emerald-50 text-emerald-800',
    bar: 'bg-emerald-500',
    fg: 'text-emerald-600',
  },
  stop: {
    label: 'Stop',
    verb: 'stop',
    past: 'stopped',
    icon: Square,
    chip: 'border-amber-200 bg-amber-50 text-amber-900',
    bar: 'bg-amber-500',
    fg: 'text-amber-600',
  },
}

export function actionMeta(action: ScheduleAction | undefined): ActionMeta {
  return ACTION_META[action ?? 'start'] ?? ACTION_META.start
}

export const STOP_MODE_LABEL: Record<StopMode, string> = {
  deallocate: 'Deallocate',
  power_off: 'Power off',
}

export const STOP_MODE_HELP: Record<StopMode, string> = {
  deallocate: 'Releases the host and stops compute billing. This is what you almost always want.',
  power_off: 'Shuts the guest down but keeps the host reserved — you are still billed for compute.',
}

export const RING_ORDER_LABEL: Record<RingOrder, string> = {
  sequence: 'Ring order (1 → n)',
  reverse: 'Reverse ring order (n → 1)',
}

/** How a wave describes itself in a sentence, e.g. "deallocate 12 VMs". */
export function actionSentence(action: ScheduleAction, stopMode: StopMode, count: number): string {
  const noun = `${count} virtual machine${count === 1 ? '' : 's'}`
  if (action === 'stop') return `${STOP_MODE_LABEL[stopMode].toLowerCase()} ${noun}`
  return `start ${noun}`
}
