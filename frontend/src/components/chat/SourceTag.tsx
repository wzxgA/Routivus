/**
 * 子任务来源标记（方案 07 §4.10）：主会话条目不渲染。
 *
 * 来源形态是 `task:<id>` / `agent:<id>`，直接显示 id 会让人不知所以，所以标成
 * 「子任务 t1」「worker a1b2c3」这种能读懂的说法。
 */
export function SourceTag({ source }: { source: string }) {
  const separator = source.indexOf(':')
  const prefix = separator > 0 ? source.slice(0, separator) : ''
  const id = (separator > 0 ? source.slice(separator + 1) : '').slice(0, 8)
  let label = source
  if (prefix === 'task') label = `子任务 ${id}`
  else if (prefix === 'agent') label = `worker ${id}`
  else if (source === 'subtask') label = '子任务'
  return (
    <span className="item-source" title={source}>
      {label}
    </span>
  )
}
