import { useMemo } from 'react'

/**
 * Inline sparkline. Deliberately tiny and unlabelled — it exists to give a KPI number
 * a shape, and the exact values live in the panels below it.
 */
export function Sparkline({ values, tone = 'blue', label }: { values: number[]; tone?: 'blue' | 'rose' | 'emerald'; label: string }) {
  const stroke = tone === 'rose' ? '#e11d48' : tone === 'emerald' ? '#059669' : '#2563eb'
  const fill = tone === 'rose' ? '#ffe4e6' : tone === 'emerald' ? '#d1fae5' : '#dbeafe'
  const width = 96
  const height = 24

  const path = useMemo(() => {
    if (values.length < 2) return null
    const peak = Math.max(...values, 1)
    const step = width / (values.length - 1)
    const points = values.map((value, index) => [index * step, height - (value / peak) * (height - 2) - 1] as const)
    const line = points.map(([x, y], index) => `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
    return { line, area: `${line} L${width},${height} L0,${height} Z` }
  }, [values])

  if (!path) return <div className="h-6" aria-hidden="true" />

  return <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} className="overflow-visible">
    <path d={path.area} fill={fill} />
    <path d={path.line} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
  </svg>
}
