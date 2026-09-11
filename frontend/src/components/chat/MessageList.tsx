import type { ReactNode } from 'react'
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
// "键：值"行淡化键名；命中成功/失败关键词的词着色；四档名着金色。

const OK_WORDS = new Set([
  '已开启', '开启', '已启用', '成功', '可用', '通过', '已保存', '完成', 'OK', '✓', 'Enabled',
])
const BAD_WORDS = new Set([
  '关闭', '失败', '不可用', '错误', '缺失', '已禁用', '未配置', 'Disabled', '(x)',
])
// 长词在前，避免"已开启"被"开启"拆散
const TOKEN_RE =
  /(已开启|已启用|已保存|已禁用|未配置|Enabled|Disabled|不可用|成功|失败|错误|缺失|开启|关闭|可用|通过|完成|OK|✓|\(x\))/g
const TIER_RE = /^(Basic|Enhanced|Superior|Ultimate)(?=\s|：|:|=)/
const KEY_RE = /^([^：:=]{1,24}[：:=])\s*(.*)$/

function renderTokens(text: string, key: string): ReactNode {
  if (!text) return '\u00a0'
  const parts = text.split(TOKEN_RE)
  return parts.map((part, index) => {
    if (OK_WORDS.has(part)) return <span key={`${key}-${index}`} className="cmd-ok">{part}</span>
    if (BAD_WORDS.has(part)) return <span key={`${key}-${index}`} className="cmd-bad">{part}</span>
    return <span key={`${key}-${index}`}>{part}</span>
  })
}

function CmdLine({ line, index }: { line: string; index: number }) {
  const key = `cmd-${index}`
  if (line === '') return <div className="cmd-line">&nbsp;</div>
  const tier = TIER_RE.exec(line)
  if (tier) {
    return (
      <div className="cmd-line">
        <span className="cmd-tier">{tier[1]}</span>
        {renderTokens(line.slice(tier[1].length), `${key}-t`)}
      </div>
    )
  }
  const match = KEY_RE.exec(line)
  if (match) {
    return (
      <div className="cmd-line">
        <span className="cmd-key">{match[1]}</span>
        {renderTokens(match[2], key)}
      </div>
    )
  }
  return <div className="cmd-line">{renderTokens(line, key)}</div>
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
      {items.map((item) => {
        switch (item.kind) {
          case 'user':
            return (
              <div className="msg-user" key={item.id}>
                {item.content}
              </div>
            )
          case 'agent':
            return (
              <div className="msg-agent" key={item.id}>
                <div className={`body${item.streaming ? ' streaming-caret' : ''}`}>
                  <Markdown text={item.content} />
                </div>
              </div>
            )
          case 'command':
            return (
              <div className={`msg-command${item.ok ? '' : ' failed'}`} key={item.id}>
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
            return (
              <div className="thinking" key={item.id}>
                {item.content}
              </div>
            )
          case 'tool':
            return <ToolCard key={item.id} item={item} />
          case 'system':
            return (
              <div className="msg-system" key={item.id}>
                {item.content}
              </div>
            )
          case 'plan':
            return <PlanCard key={item.id} payload={item.payload} />
          case 'team':
            return <TeamCard key={item.id} payload={item.payload} />
          default:
            return null
        }
      })}
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
