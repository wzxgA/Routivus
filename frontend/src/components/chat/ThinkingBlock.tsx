import { useState } from 'react'
import type { ThinkingItem } from '../../state/sessionTimeline'
import { SourceTag } from './SourceTag'

/**
 * 思考块：流式期间展开（能看见它在想），段收尾后自动折叠成一行（方案 07 §4.4）。
 *
 * 折叠状态是组件内 state（不写回 item，保住 ChatRow 的 memo）。流式条目与落定
 * 条目的 id 不同（流式 id 带 stream- 前缀，落定 id 是消息 id），段收尾时组件
 * 重建、自然回到"默认折叠"——所以不需要任何额外的事件来收起。
 *
 * 只显示字数、不显示"用时"：用时只有在线事件里有，刷新后拿不到，会造出
 * "在线有秒数、回看没有"的新不一致；字数从内容长度就能算，两侧一致。
 */
export function ThinkingBlock({ item }: { item: ThinkingItem }) {
  const [expanded, setExpanded] = useState(Boolean(item.streaming))
  return (
    <div className={`thinking-block${expanded ? ' expanded' : ''}`}>
      <button type="button" className="thinking-head" onClick={() => setExpanded((value) => !value)}>
        <span className="thinking-caret">{expanded ? '▾' : '▸'}</span>
        <span className="thinking-label">思考 · {item.content.length.toLocaleString()} 字</span>
        {item.source ? <SourceTag source={item.source} /> : null}
        {item.streaming ? <span className="thinking-live">思考中…</span> : null}
      </button>
      {expanded ? <div className="thinking-body">{item.content}</div> : null}
    </div>
  )
}
