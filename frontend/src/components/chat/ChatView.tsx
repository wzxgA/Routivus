import { useCallback, useEffect, useRef, useState } from 'react'
import type { Session } from '../../api/types'
import { useSessionTimeline } from '../../state/sessionTimeline'
import type { ConnState } from '../../ws/sessionSocket'
import { Empty } from '../common/Empty'
import { TerminalDrawer } from '../terminal/TerminalDrawer'
import { Composer } from './Composer'
import { MessageList } from './MessageList'
import { SidePanel } from './SidePanel'

interface ChatViewProps {
  projectId: string
  projectName: string
  projectPath: string | null
  session: Session | null
  projectNotesCount: number
  terminalOpen: boolean
  contextWindow: number
  onToggleTerminal: () => void
  onSessionUpdate: (session: Session) => void
  onHitlChange: (hitl: string | null) => void
  onConnectionChange: (state: ConnState) => void
}

export function ChatView({
  projectId,
  projectName,
  projectPath,
  session,
  projectNotesCount,
  terminalOpen,
  contextWindow,
  onToggleTerminal,
  onSessionUpdate,
  onHitlChange,
  onConnectionChange,
}: ChatViewProps) {
  const timeline = useSessionTimeline(projectId, session?.id ?? null, session, onSessionUpdate)
  const [composer, setComposer] = useState('')
  const [history, setHistory] = useState<string[]>([])
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const pinnedToBottom = useRef(true)

  const liveSession = timeline.session ?? session

  const latestPlan = (() => {
    for (let i = timeline.items.length - 1; i >= 0; i -= 1) {
      const item = timeline.items[i]
      if (item.kind === 'plan') return item.payload
    }
    return null
  })()

  const handleScroll = useCallback(() => {
    const element = scrollRef.current
    if (!element) return
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight
    pinnedToBottom.current = distance < 80
  }, [])

  useEffect(() => {
    const element = scrollRef.current
    if (element && pinnedToBottom.current) {
      element.scrollTop = element.scrollHeight
    }
  }, [timeline.items])

  // 把服务端上报的 HITL 状态同步给顶栏 chips（快照内的 safety.hitl）。
  useEffect(() => {
    onHitlChange(timeline.hitl)
  }, [timeline.hitl, onHitlChange])

  useEffect(() => {
    onConnectionChange(timeline.connection)
  }, [timeline.connection, onConnectionChange])

  const handleSubmit = useCallback(() => {
    const text = composer.trim()
    if (!text) return
    timeline.sendMessage(text)
    setHistory((current) => [...current, text])
    setComposer('')
    pinnedToBottom.current = true
  }, [composer, timeline])

  const cancelSession = useCallback(() => {
    timeline.cancel()
  }, [timeline])

  return (
    <section className="view chat-view">
      <div className="chat">
        {timeline.error ? (
          <div className="banner error">
            <span>{timeline.error}</span>
            <span className="spacer" />
            <button type="button" className="link-btn" onClick={timeline.clearError}>
              关闭
            </button>
          </div>
        ) : null}

        <div className="chat-scroll" ref={scrollRef} onScroll={handleScroll}>
          {timeline.items.length === 0 ? (
            <Empty
              title={session ? '开始新的任务' : '没有可用的会话'}
              hint={
                session
                  ? '直接输入即普通对话；/plan <任务> 生成计划后审阅执行，/team <任务> 调度多 Agent 协作。危险操作会先请求审批。'
                  : '正在为新项目创建首个会话，请稍候…'
              }
            />
          ) : (
            <MessageList
              items={timeline.items}
              approval={timeline.approval}
              planReview={timeline.planReview}
              onResolveApproval={timeline.resolveApproval}
              onAnswerAsk={timeline.answerAsk}
              onDecideReview={timeline.resolvePlanReview}
            />
          )}
        </div>

        <TerminalDrawer
          projectId={projectId}
          open={terminalOpen}
          cwd={projectPath}
          onRequestClose={onToggleTerminal}
        />

        <Composer
          prompt={projectName}
          value={composer}
          history={history}
          status={liveSession?.status ?? null}
          disabled={timeline.connection === 'offline' || !session}
          waitingApproval={Boolean(timeline.approval)}
          onChange={setComposer}
          onSubmit={handleSubmit}
          onCancel={cancelSession}
        />
      </div>

      <SidePanel
        session={liveSession}
        usage={timeline.usage}
        audit={timeline.audit}
        connection={timeline.connection}
        plan={latestPlan}
        projectPath={projectPath}
        projectNotes={projectNotesCount}
        memoryNotice={timeline.memoryNotice}
        hitl={timeline.hitl}
        contextWindow={contextWindow}
      />
    </section>
  )
}
