import type { TimelineItem } from '../../state/sessionTimeline'
import { Markdown } from '../../utils/markdown'
import { ApprovalCard } from './ApprovalCard'
import { PlanCard } from './PlanCard'
import { TeamCard } from './TeamCard'
import { ToolCard } from './ToolCard'
import type { ApprovalRequestedData } from '../../api/types'

interface MessageListProps {
  items: TimelineItem[]
  approval: ApprovalRequestedData | null
  onInsertCommand: (command: string) => void
  onResolveApproval: (
    decision: 'approve' | 'reject',
    options?: { args?: Record<string, unknown>; scope?: 'session' },
  ) => void
  onAnswerAsk: (answers: Record<string, string> | null) => void
}

export function MessageList({
  items,
  approval,
  onInsertCommand,
  onResolveApproval,
  onAnswerAsk,
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
            return <PlanCard key={item.id} payload={item.payload} onInsertCommand={onInsertCommand} />
          case 'team':
            return <TeamCard key={item.id} payload={item.payload} onInsertCommand={onInsertCommand} />
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
    </>
  )
}
