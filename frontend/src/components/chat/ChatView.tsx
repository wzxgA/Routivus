import { useCallback, useEffect, useRef, useState } from 'react'
import type { RouterState, Session } from '../../api/types'
import { useSessionTimeline } from '../../state/sessionTimeline'
import type { ConnState } from '../../ws/sessionSocket'
import { Empty } from '../common/Empty'
import { TerminalDrawer } from '../terminal/TerminalDrawer'
import { Composer, type ComposerMode } from './Composer'
import { MessageList } from './MessageList'
import { SidePanel } from './SidePanel'

/**
 * 模式 → 服务端能识别的指令前缀。
 *
 * 用户输入里已经带 `/` 的命令原样放行（尊重显式命令，避免 `/plan /team x` 这种叠加）。
 */
function composeCommand(mode: ComposerMode, text: string): string {
  if (text.startsWith('/')) return text
  if (mode === 'plan') return `/plan ${text}`
  if (mode === 'team') return `/team ${text}`
  return text
}

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
  onRouterChange: (router: RouterState | null) => void
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
  onRouterChange,
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

  // 智能路由档位同步给顶栏（普通对话轮按复杂度换档的结果）
  useEffect(() => {
    onRouterChange(timeline.router)
  }, [timeline.router, onRouterChange])

  const handleSubmit = useCallback(
    (mode: ComposerMode) => {
      const text = composer.trim()
      if (!text) return
      timeline.sendMessage(composeCommand(mode, text))
      // 历史里留用户实际输入的文本，不带模式前缀，便于直接复现
      setHistory((current) => [...current, text])
      setComposer('')
      pinnedToBottom.current = true
    },
    [composer, timeline],
  )

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
          sessionId={session?.id ?? null}
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
        memory={timeline.memory}
        onRefreshMemory={timeline.refreshMemory}
        memoryNotice={timeline.memoryNotice}
        hitl={timeline.hitl}
        router={timeline.router}
        contextWindow={contextWindow}
      />
    </section>
  )
}
