const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'] as const

export function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function pad(value: number): string {
  return value < 10 ? `0${value}` : String(value)
}

/** 本地日期键（YYYY-MM-DD），与服务端 /api/activity 的 date 字段一致。 */
export function toDateKey(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

export function formatTime(value: string | null | undefined): string {
  const date = parseDate(value)
  if (!date) return ''
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function formatRelative(value: string | null | undefined): string {
  const date = parseDate(value)
  if (!date) return ''
  const diffMs = Date.now() - date.getTime()
  const minutes = Math.floor(diffMs / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days} 天前`
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

export function formatDateTime(value: string | null | undefined): string {
  const date = parseDate(value)
  if (!date) return ''
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`
}

export function formatDateLong(value: string | Date | null | undefined): string {
  const date = typeof value === 'string' ? parseDate(value) : (value ?? null)
  if (!date) return ''
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`
}

export function weekdayLabel(value: string | Date): string {
  const date = typeof value === 'string' ? parseDate(value) : value
  if (!date) return ''
  return WEEKDAYS[date.getDay()]
}

export function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN')
}

export function truncate(value: string, max: number): string {
  if (value.length <= max) return value
  return `${value.slice(0, max - 1)}…`
}

/** 把 token 数折算成 k/M 可读形式。 */
export function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return String(value)
}

export function countWords(text: string): number {
  return text.replace(/\s+/g, '').length
}
