import { useState } from 'react'
import type { ToolItem } from '../../state/sessionTimeline'
import { truncate } from '../../utils/format'
import { SourceTag } from './SourceTag'

/** 超过这个行数的输出默认折叠（方案 07 批次 1：替换原先 max-height 的硬截断）。 */
const FOLD_LINES = 12

export function ToolCard({ item }: { item: ToolItem }) {
  const pending = item.ok === null
  // 展开状态是组件内 state（不写回 item，保住 ChatRow 的 memo，方案 07 §4.8）。
  const [expanded, setExpanded] = useState(false)
  const body = pending
    ? ''
    : item.ok
      ? item.output || '（无输出）'
      : item.error || item.output || '执行失败'
  const lines = body.split('\n')
  const foldable = !pending && lines.length > FOLD_LINES
  const shown = foldable && !expanded ? lines.slice(0, FOLD_LINES).join('\n') : body
  return (
    <div className="tool">
      <div className="tool-head">
        <span className="tool-name">{item.name}</span>
        <span className="tool-args" title={item.args}>
          {truncate(item.args.replace(/\s+/g, ' '), 120)}
        </span>
        {item.source ? <SourceTag source={item.source} /> : null}
        <span className="tool-time">
          {pending ? '执行中…' : item.durationMs !== null ? `${item.durationMs} ms` : ''}
        </span>
      </div>
      <div className="tool-body">
        {pending ? (
          <>
            <span className="tool-pending">●</span>等待工具返回…
          </>
        ) : (
          <>
            <span className={item.ok ? 'tool-ok' : 'tool-fail'}>{item.ok ? '✓' : '✗'}</span>
            {shown}
          </>
        )}
      </div>
      {foldable ? (
        <button type="button" className="tool-fold" onClick={() => setExpanded((value) => !value)}>
          {expanded ? '收起' : `展开全部（共 ${lines.length} 行）`}
        </button>
      ) : null}
    </div>
  )
}
