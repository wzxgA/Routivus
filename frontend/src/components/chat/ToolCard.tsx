import type { ToolItem } from '../../state/sessionTimeline'
import { truncate } from '../../utils/format'

export function ToolCard({ item }: { item: ToolItem }) {
  const pending = item.ok === null
  return (
    <div className="tool">
      <div className="tool-head">
        <span className="tool-name">{item.name}</span>
        <span className="tool-args" title={item.args}>
          {truncate(item.args.replace(/\s+/g, ' '), 120)}
        </span>
        <span className="tool-time">
          {pending ? '执行中…' : item.durationMs !== null ? `${item.durationMs} ms` : ''}
        </span>
      </div>
      <div className="tool-body">
        {pending ? (
          <>
            <span className="tool-pending">●</span>等待工具返回…
          </>
        ) : item.ok ? (
          <>
            <span className="tool-ok">✓</span>
            {item.output || '（无输出）'}
          </>
        ) : (
          <>
            <span className="tool-fail">✗</span>
            {item.error || item.output || '执行失败'}
          </>
        )}
      </div>
    </div>
  )
}
