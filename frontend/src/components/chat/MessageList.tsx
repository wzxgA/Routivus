import { memo, type ReactNode } from 'react'
import type { TimelineItem } from '../../state/sessionTimeline'
import { Markdown } from '../../utils/markdown'
import { ApprovalCard } from './ApprovalCard'
import { PlanCard } from './PlanCard'
import { PlanReviewCard } from './PlanReviewCard'
import { TeamCard } from './TeamCard'
import { ToolCard } from './ToolCard'
import type { ApprovalRequestedData, PlanReviewRequest } from '../../api/types'

// ---- 命令回执的语义高亮 -------------------------------------------------
// 命令输出是纯文本（service 层拼的表格/状态行），这里按行做轻量着色：
// - "键：值"行：键名淡化
// - 空格分列的表格行（≥2 列）：行首列（档位/provider 名）金色加粗
// - k=v 形式的 token：键部分淡化、值正常
// - 成功/失败关键词着绿/红；纯数字淡化
// 空白分隔符原样保留（pre 下表格对齐不破坏）。

const OK_WORDS = new Set([
  '已开启', '开启', '已启用', '成功', '可用', '通过', '已保存', '完成', 'OK', '✓', 'Enabled',
])
const BAD_WORDS = new Set([
  '关闭', '失败', '不可用', '错误', '缺失', '已禁用', '未配置', 'Disabled', '(x)',
])
const TIER_RE = /^(Basic|Enhanced|Superior|Ultimate)(?=\s|：|:|=)/
const KEY_RE = /^([^：:=]{1,24}[：:=])\s*(.*)$/
const NUM_RE = /^\d+(?:[.,]\d+)?$/
const WS_SPLIT = /(\s+)/

function renderTokens(text: string, key: string, lead = false): ReactNode {
  if (!text) return '\u00a0'
  let leadPending = lead
  return text.split(WS_SPLIT).map((part, index) => {
    const tokenKey = `${key}-${index}`
    if (part === '' || /^\s+$/.test(part)) return <span key={tokenKey}>{part}</span>
    const isLead = leadPending
    leadPending = false
    if (isLead) return <span key={tokenKey} className="cmd-tier">{part}</span>
    const eq = part.indexOf('=')
    if (eq > 0) {
      return (
        <span key={tokenKey}>
          <span className="cmd-kdim">{part.slice(0, eq + 1)}</span>
          {part.slice(eq + 1)}
        </span>
      )
    }
    if (OK_WORDS.has(part)) return <span key={tokenKey} className="cmd-ok">{part}</span>
    if (BAD_WORDS.has(part)) return <span key={tokenKey} className="cmd-bad">{part}</span>
    if (NUM_RE.test(part)) return <span key={tokenKey} className="cmd-num">{part}</span>
    return <span key={tokenKey}>{part}</span>
  })
}

function CmdLine({ line, index }: { line: string; index: number }) {
  const key = `cmd-${index}`
  if (line === '') return <div className="cmd-line">&nbsp;</div>
  // 四档名开头的行：档位名金色，其余按 token 着色
  const tier = TIER_RE.exec(line)
  if (tier) {
    return (
      <div className="cmd-line">
        <span className="cmd-tier">{tier[1]}</span>
        {renderTokens(line.slice(tier[1].length), `${key}-t`)}
      </div>
    )
  }
  // "键：值"行：键名淡化，值部分照常着色
  const match = KEY_RE.exec(line)
  if (match) {
    return (
      <div className="cmd-line">
        <span className="cmd-key">{match[1]}</span>
        {renderTokens(match[2], key)}
      </div>
    )
  }
  // 表格行（≥2 列才认定为表格，避免中文整句被误染）：行首列金色
  const columns = line.trim().split(/\s+/)
  return <div className="cmd-line">{renderTokens(line, key, columns.length >= 2)}</div>
}

interface MessageListProps {
  items: TimelineItem[]
  approval: ApprovalRequestedData | null
  planReview: PlanReviewRequest | null
  onResolveApproval: (
    decision: 'approve' | 'reject',
    options?: { args?: Record<string, unknown>; scope?: 'session' },
  ) => void
  onAnswerAsk: (answers: Record<string, string> | null) => void
  onDecideReview: (action: 'execute' | 'cancel' | 'replan', feedback?: string) => void
}

/**
 * 单行消息。用 `memo` 包住是**性能关键**：流式追加时父组件每一帧都渲染，若不冻结
 * 已完成的行，屏幕上每条历史回答都会跟着重新跑一遍 Markdown 解析与着色。
 *
 * 可靠性前提：`sessionTimeline` 里所有对 item 的更新都是新建对象（`map` 出新对象、
 * `upsertTaskCard` 返回新数组），没有原地改字段——**原地改会让 memo 静默失效**。
 */
const ChatRow = memo(function ChatRow({ item }: { item: TimelineItem }) {
  switch (item.kind) {
    case 'user':
      return <div className="msg-user">{item.content}</div>
    case 'agent':
      return (
        <div className="msg-agent">
          <div className={`body${item.streaming ? ' streaming-caret' : ''}`}>
            <Markdown text={item.content} streaming={Boolean(item.streaming)} />
          </div>
        </div>
      )
    case 'command':
      return (
        <div className={`msg-command${item.ok ? '' : ' failed'}`}>
          <div className="msg-command-head">
            <span className="badge">{item.ok ? '命令' : '命令失败'}</span>
            <code>{item.command}</code>
          </div>
          {/* 命令输出是多行对齐的纯文本表格：不能用 Markdown（会折叠换行）。
              深色终端面板 + 逐行语义着色，见文件顶部 CmdLine。 */}
          <div className="msg-command-body">
            {item.content.split('\n').map((line, index) => (
              <CmdLine key={index} line={line} index={index} />
            ))}
          </div>
        </div>
      )
    case 'thinking':
      return <div className="thinking">{item.content}</div>
    case 'tool':
      return <ToolCard item={item} />
    case 'system':
      return <div className="msg-system">{item.content}</div>
    case 'plan':
      return <PlanCard payload={item.payload} />
    case 'team':
      return <TeamCard payload={item.payload} />
    default:
      return null
  }
})

export function MessageList({
  items,
  approval,
  planReview,
  onResolveApproval,
  onAnswerAsk,
  onDecideReview,
}: MessageListProps) {
  return (
    <>
      {items.map((item) => (
        <ChatRow key={item.id} item={item} />
      ))}
      {approval ? (
        <ApprovalCard
          approval={approval}
          key={approval.approval_id ?? 'pending-approval'}
          onResolve={onResolveApproval}
          onAnswer={onAnswerAsk}
        />
      ) : null}
      {planReview ? (
        <PlanReviewCard
          review={planReview}
          key={planReview.review_id || 'pending-plan-review'}
          onDecide={onDecideReview}
        />
      ) : null}
    </>
  )
}
