import { useCallback, useMemo, useState } from 'react'

export type SortDirection = 'asc' | 'desc'
export type Sort = { key: string; direction: SortDirection }

/**
 * Sort state for a grid. Clicking the active column flips direction; a new column
 * starts at the caller's preferred direction for that column.
 */
export function useSort(initial: Sort, descFirst: string[] = []) {
  const [sort, setSort] = useState<Sort>(initial)
  const toggle = useCallback((key: string) => {
    setSort((current) => (current.key === key
      ? { key, direction: current.direction === 'asc' ? 'desc' : 'asc' }
      : { key, direction: descFirst.includes(key) ? 'desc' : 'asc' }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [descFirst.join(',')])
  const params = useMemo(() => `sort=${encodeURIComponent(sort.key)}&direction=${sort.direction}`, [sort])
  return { sort, toggle, params }
}

/** Undefined and null always sort last, whichever direction is active. */
function compare(a: unknown, b: unknown): number {
  const aEmpty = a === null || a === undefined || a === ''
  const bEmpty = b === null || b === undefined || b === ''
  if (aEmpty && bEmpty) return 0
  if (aEmpty) return 1
  if (bEmpty) return -1
  if (typeof a === 'number' && typeof b === 'number') return a - b
  if (typeof a === 'boolean' && typeof b === 'boolean') return Number(a) - Number(b)
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' })
}

/** Client-side sorting for grids that already hold every row. */
export function sortRows<T>(rows: T[], accessor: ((row: T) => unknown) | undefined, direction: SortDirection): T[] {
  if (!accessor) return rows
  const sign = direction === 'desc' ? -1 : 1
  return [...rows].sort((a, b) => {
    const result = compare(accessor(a), accessor(b))
    // Empty values stay last rather than flipping to the top when the direction changes.
    return result === 0 ? 0 : sign * result
  })
}

export function useClientSort<T>(rows: T[], accessors: Record<string, (row: T) => unknown>, initial: Sort, descFirst?: string[]) {
  const { sort, toggle } = useSort(initial, descFirst)
  const sorted = useMemo(() => sortRows(rows, accessors[sort.key], sort.direction), [rows, accessors, sort])
  return { rows: sorted, sort, toggle }
}
