import type { TimelineItem } from '../../state/sessionTimeline'
import { Markdown } from '../../utils/markdown'
import { ApprovalCard } from './ApprovalCard'
import { PlanCard } from './PlanCard'
import { PlanReviewCard } from './PlanReviewCard'
import { TeamCard } from './TeamCard'
import { ToolCard } from './ToolCard'
import type { ApprovalRequestedData, PlanReviewRequest } from '../../api/types'

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
                <div className="body">
                  <Markdown text={item.content} />
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
