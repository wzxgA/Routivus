import { useMemo, useState } from 'react'
import type { ActivityDay } from '../../api/types'
import { formatDateLong, toDateKey } from '../../utils/format'

const WEEKS = 52
const CELL = 12
const GAP = 2

interface HeatCell {
  key: string
  date: Date
  level: number
  count: number
  projectCount: number
  future: boolean
}

interface TooltipState {
  x: number
  y: number
  label: string
}

function levelFor(count: number, max: number): number {
  if (count <= 0) return 0
  if (max <= 0) return 1
  const ratio = count / max
  if (ratio <= 0.25) return 1
  if (ratio <= 0.5) return 2
  if (ratio <= 0.75) return 3
  return 4
}

/**
 * 全部项目活动热力图（仅首页）。52 周 × 7 天，周一为每列第一行；
 * 未来日期不渲染数据格，避免误导。
 */
export function Heatmap({ days }: { days: ActivityDay[] }) {
  const [tooltip, setTooltip] = useState<TooltipState | null>(null)

  const { columns, monthLabels } = useMemo(() => {
    const map = new Map<string, ActivityDay>()
    let max = 0
    for (const day of days) {
      map.set(day.date, day)
      if (day.count > max) max = day.count
    }

    const today = new Date()
    today.setHours(0, 0, 0, 0)
    const mondayOffset = (today.getDay() + 6) % 7
    const currentMonday = new Date(today)
    currentMonday.setDate(today.getDate() - mondayOffset)
    const gridStart = new Date(currentMonday)
    gridStart.setDate(currentMonday.getDate() - (WEEKS - 1) * 7)

    const cols: HeatCell[][] = []
    const labels: { left: number; text: string }[] = []
    let lastMonth = -1

    for (let c = 0; c < WEEKS; c += 1) {
      const column: HeatCell[] = []
      for (let r = 0; r < 7; r += 1) {
        const date = new Date(gridStart)
        date.setDate(gridStart.getDate() + c * 7 + r)
        const key = toDateKey(date)
        const entry = map.get(key)
        const future = date.getTime() > today.getTime()
        column.push({
          key,
          date,
          level: future ? 0 : levelFor(entry?.count ?? 0, max),
          count: entry?.count ?? 0,
          projectCount: entry ? Object.keys(entry.projects).length : 0,
          future,
        })
      }
      cols.push(column)

      const firstOfColumn = column[0].date
      if (firstOfColumn.getMonth() !== lastMonth && firstOfColumn.getDate() <= 7) {
        labels.push({
          left: c * (CELL + GAP),
          text: `${firstOfColumn.getMonth() + 1}月`,
        })
      }
      lastMonth = firstOfColumn.getMonth()
    }

    return { columns: cols, monthLabels: labels }
  }, [days])

  const showTooltip = (event: React.MouseEvent<HTMLDivElement>, cell: HeatCell) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const label = cell.future
      ? '未来日期'
      : cell.count > 0
        ? `${formatDateLong(cell.date)} · ${cell.count} 次活动 · 涉及 ${cell.projectCount} 个项目`
        : `${formatDateLong(cell.date)} · 无记录`
    setTooltip({ x: rect.left + rect.width / 2, y: rect.top - 6, label })
  }

  return (
    <div className="heat-card">
      <div className="card-head">
        <div className="card-name">全部项目活动 · 近一年</div>
      </div>
      <div className="heat-scroll">
        <div className="heat-months" style={{ width: WEEKS * (CELL + GAP), height: 13 }}>
          {monthLabels.map((label) => (
            <span key={`${label.text}-${label.left}`} style={{ left: label.left }}>
              {label.text}
            </span>
          ))}
        </div>
        <div className="heat-cols">
          {columns.map((column, index) => (
            <div className="heat-col" key={`col-${index}`}>
              {column.map((cell) => (
                <div
                  key={cell.key}
                  className="heat-cell"
                  data-l={cell.level}
                  onMouseEnter={(event) => showTooltip(event, cell)}
                  onMouseLeave={() => setTooltip(null)}
                />
              ))}
            </div>
          ))}
        </div>
      </div>
      <div className="heat-legend">
        活跃较少
        {[0, 1, 2, 3, 4].map((level) => (
          <div key={level} className="heat-cell" data-l={level} />
        ))}
        活跃较多
      </div>
      {tooltip ? (
        <div className="heat-tip show" style={{ left: tooltip.x, top: tooltip.y }}>
          {tooltip.label}
        </div>
      ) : null}
    </div>
  )
}
