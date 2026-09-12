import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { fetchSessionMemory } from '../api'
import { newRequestId } from '../api/client'
import type {
  ApprovalRequestedData,
  AskRequest,
  MemoryPayload,
  Message,
  PlanPayload,
  PlanReviewRequest,
  RouterState,
  Session,
  SessionSnapshot,
  TeamPayload,
  WsEnvelope,
} from '../api/types'
import { StreamBatcher } from '../utils/streamBatch'
import { SessionSocket, type ConnState } from '../ws/sessionSocket'

export interface ToolItem {
  kind: 'tool'
  id: string
  name: string
  args: string
  ok: boolean | null
  output: string
  error: string
  durationMs: number | null
  at: string
}

export interface PlanItem {
  kind: 'plan'
  id: string
  payload: PlanPayload
}

export interface TeamItem {
  kind: 'team'
  id: string
  payload: TeamPayload
}

export type TimelineItem =
  | { kind: 'user'; id: string; content: string; at: string }
  | { kind: 'agent'; id: string; content: string; at: string; streaming?: boolean }
  | { kind: 'thinking'; id: string; content: string }
  | { kind: 'system'; id: string; content: string; at: string }
  | { kind: 'command'; id: string; command: string; content: string; ok: boolean; at: string }
  | ToolItem
  | PlanItem
  | TeamItem

export interface UsageTotals {
  prompt: number
  completion: number
  total: number
}

export interface AuditTotals {
  tool_calls: number
  tool_failures: number
  approvals: number
}

export interface SessionTimelineValue {
  items: TimelineItem[]
  stream: { content: string; thinking: string }
  connection: ConnState
  audit: AuditTotals
  usage: UsageTotals
  session: Session | null
  approval: ApprovalRequestedData | null
  planReview: PlanReviewRequest | null
  router: RouterState | null
  memory: MemoryPayload | null
  memoryNotice: { kind: string; message: string } | null
  hitl: string | null
  error: string | null
  refreshMemory: () => void
  sendMessage: (content: string) => void
  cancel: () => void
  resolveApproval: (
    decision: 'approve' | 'reject',
    options?: { args?: Record<string, unknown>; scope?: 'session' },
  ) => void
  answerAsk: (answers: Record<string, string> | null) => void
  resolvePlanReview: (action: 'execute' | 'cancel' | 'replan', feedback?: string) => void
  clearError: () => void
}

const STREAM_AGENT_ID = 'stream-agent'
const STREAM_THINK_ID = 'stream-thinking'

function messageToItem(message: Message): TimelineItem | null {
  switch (message.role) {
    case 'user':
      return { kind: 'user', id: message.id, content: message.content, at: message.created_at }
    case 'assistant':
      return { kind: 'agent', id: message.id, content: message.content, at: message.created_at }
    case 'tool':
      // 工具卡不再从消息表重建（消息里没有参数/成败/耗时，只能画出残缺卡），
      // 统一由快照 replay 的 tool.started/tool.completed 事件还原，避免重复卡片。
      return null
    case 'system':
      return { kind: 'system', id: message.id, content: message.content, at: message.created_at }
    default:
      return null
  }
}

/**
 * 会话事件流：把 WebSocket 事件归约为可渲染的时间线。
 *
 * 断线恢复策略：每次（重）连服务端都会推送完整 `session.snapshot`，前端以快照
 * 为准重建——消息来自 `snapshot.messages`，卡片（命令 / 工具 / 计划 / 团队）
 * 由 `snapshot.replay` 的历史事件按序回放给同一个 reducer 重建，仍挂起的交互
 * 由 `snapshot.pending` 恢复成可继续应答的卡片；此后按 `sequence` 严格递增
 * 应用增量事件。工具卡只认事件（消息表里的 tool 角色缺少参数/成败/耗时）。
 */
export function useSessionTimeline(
  projectId: string | null,
  sessionId: string | null,
  initialSession: Session | null,
  onSessionUpdate: (session: Session) => void,
): SessionTimelineValue {
  const [items, setItems] = useState<TimelineItem[]>([])
  const [stream, setStream] = useState({ content: '', thinking: '' })
  const [connection, setConnection] = useState<ConnState>('connecting')
  const [audit, setAudit] = useState<AuditTotals>({
    tool_calls: 0,
    tool_failures: 0,
    approvals: 0,
  })
  const [usage, setUsage] = useState<UsageTotals>({ prompt: 0, completion: 0, total: 0 })
  const [session, setSession] = useState<Session | null>(initialSession)
  const [approval, setApproval] = useState<ApprovalRequestedData | null>(null)
  const [planReview, setPlanReview] = useState<PlanReviewRequest | null>(null)
  const [router, setRouter] = useState<RouterState | null>(null)
  const [memory, setMemory] = useState<MemoryPayload | null>(null)
  const [memoryNotice, setMemoryNotice] = useState<{ kind: string; message: string } | null>(null)
  const [hitl, setHitl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const socketRef = useRef<SessionSocket | null>(null)
  const sessionRef = useRef<Session | null>(initialSession)
  const updateRef = useRef(onSessionUpdate)

  /** 重拉项目长期记忆条目（快照已带一份，这里用于命令改动后刷新）。 */
  const refreshMemory = useCallback(() => {
    if (!sessionId) return
    fetchSessionMemory(sessionId)
      .then((data) => setMemory(data))
      .catch(() => {
        /* 附加只读信息：失败静默，不打扰会话主流程 */
      })
  }, [sessionId])

  // 事件回调要用最新的 refreshMemory，但又不能把它塞进 socket effect 的依赖
  // （会导致每次刷新都重建连接），照 updateRef 的做法走 ref。
  const refreshMemoryRef = useRef(refreshMemory)
  useEffect(() => {
    refreshMemoryRef.current = refreshMemory
  }, [refreshMemory])

  // 在 effect 内同步最新的回调，避免渲染期间写入 ref。
  // 该 effect 声明在 socket effect 之前，因此连接建立前 updateRef 已是最新值。
  useEffect(() => {
    updateRef.current = onSessionUpdate
  }, [onSessionUpdate])

  useEffect(() => {
    setSession(initialSession)
    sessionRef.current = initialSession
  }, [initialSession])

  useEffect(() => {
    if (!projectId || !sessionId) {
      setConnection('offline')
      return
    }

    // 流式文本先攒批再刷出：模型逐 token 吐字，每个 token 都触发一次重型渲染
    // （Markdown 解析成本随正文长度线性增长），攒批把渲染次数封顶。
    // 局限：只减少次数、不降低单次成本，见 utils/streamBatch.ts 与方案 §11。
    const batch = new StreamBatcher((delta) => {
      setStream((current) => ({
        content: current.content + delta.content,
        thinking: current.thinking + delta.thinking,
      }))
    })

    /** 清空流式区：连同攒批里的尾巴一起丢掉（否则定时器到点会把旧内容补回来）。 */
    const resetStream = () => {
      batch.reset()
      setStream({ content: '', thinking: '' })
    }

    // 切换会话：清空上一会话的时间线，避免串数据。
    setItems([])
    resetStream()
    setApproval(null)
    setPlanReview(null)
    setRouter(null)
    setMemory(null)
    setMemoryNotice(null)
    setError(null)

    // 事件归约：在线增量与快照回放共用同一套 reducer，避免两套渲染逻辑漂移。
    const applyEvent = (event: WsEnvelope) => {
        const data = (event.data ?? {}) as Record<string, unknown>
        switch (event.type) {
          case 'session.snapshot': {
            const snapshot = data as unknown as SessionSnapshot
            const restored = (snapshot.messages ?? [])
              .map(messageToItem)
              .filter((item): item is TimelineItem => item !== null)
            setItems(restored)
            resetStream()
            setAudit(snapshot.audit ?? { tool_calls: 0, tool_failures: 0, approvals: 0 })
            setHitl(snapshot.safety?.hitl ?? null)
            setRouter(snapshot.router ?? null)
            // 项目长期记忆条目随快照下发，重连/切会话即可见。
            setMemory(snapshot.memory ?? null)
            const snapshotSession = snapshot.session ?? null
            if (snapshotSession) {
              setSession(snapshotSession)
              sessionRef.current = snapshotSession
              setUsage({
                prompt: snapshotSession.prompt_tokens,
                completion: snapshotSession.completion_tokens,
                total: snapshotSession.total_tokens,
              })
              updateRef.current(snapshotSession)
            }
            // 待决交互：内存桥仍挂着 → 恢复成"可以继续应答"的卡片
            // （不恢复的话，重连后审批卡消失，用户只能干等超时 fail closed）。
            setApproval(snapshot.pending?.approval ?? null)
            setPlanReview(snapshot.pending?.plan_review ?? null)
            // 卡片类历史事件回放：结构化状态不落消息表，用快照带回的事件重建
            // （命令卡 / 工具卡 / 计划卡 / 团队卡），喂给同一个 reducer。
            for (const replayed of snapshot.replay ?? []) {
              applyEvent({
                type: replayed.type,
                sequence: replayed.sequence,
                data: replayed.data,
              } as WsEnvelope)
            }
            return
          }
          case 'message.created': {
            const message = data.message as Message | undefined
            if (!message) return
            // 命令回执也是 assistant 消息（run_command_turn 落库后广播），
            // 不能再像用户输入那样渲染成右侧气泡。
            if (message.role === 'user') {
              setItems((current) => [...current, { kind: 'user', id: message.id, content: message.content, at: message.created_at }])
            } else {
              setItems((current) => [...current, { kind: 'agent', id: message.id, content: message.content, at: message.created_at }])
            }
            return
          }
          case 'command.executed': {
            // 把回执条目标记为命令结果（ok 决定成败配色）。优先按 message_id
            // 精确定位——回放时"最近一条 agent"的启发式不再可靠。
            const command = String(data.command ?? '')
            const ok = Boolean(data.ok)
            const messageId = String(data.message_id ?? '')
            setItems((current) => {
              let index = -1
              if (messageId) {
                index = current.findIndex((item) => item.id === messageId)
              } else {
                for (let cursor = current.length - 1; cursor >= 0; cursor -= 1) {
                  if (current[cursor]?.kind === 'agent') {
                    index = cursor
                    break
                  }
                }
              }
              const item = index >= 0 ? current[index] : undefined
              if (!item || item.kind !== 'agent') return current
              const next = [...current]
              next[index] = { kind: 'command', id: item.id, command, content: item.content, ok, at: item.at }
              return next
            })
            return
          }
          case 'message.delta': {
            batch.push(String(data.kind ?? 'content'), String(data.text ?? ''))
            return
          }
          case 'message.completed': {
            const message = data.message as Message | undefined
            // 完整正文由这条消息接管，攒批里的尾巴直接丢弃（避免重复补上）。
            resetStream()
            if (message) {
              setItems((current) => [
                ...current,
                { kind: 'agent', id: message.id, content: message.content, at: message.created_at },
              ])
            }
            return
          }
          case 'tool.started': {
            const toolCallId = String(data.tool_call_id ?? '')
            setItems((current) => [
              ...current,
              {
                kind: 'tool',
                id: toolCallId || `tool-${current.length}`,
                name: String(data.name ?? 'tool'),
                args: String(data.arguments ?? ''),
                ok: null,
                output: '',
                error: '',
                durationMs: null,
                at: new Date().toISOString(),
              },
            ])
            return
          }
          case 'tool.completed': {
            const toolCallId = String(data.tool_call_id ?? '')
            setItems((current) =>
              current.map((item) =>
                item.kind === 'tool' && item.id === toolCallId
                  ? {
                      ...item,
                      name: String(data.name ?? item.name),
                      ok: Boolean(data.ok),
                      output: String(data.output ?? ''),
                      error: String(data.error ?? ''),
                      durationMs: Number(data.duration_ms ?? 0),
                    }
                  : item,
              ),
            )
            return
          }
          case 'audit.updated': {
            setAudit((current) => ({
              ...current,
              tool_calls: Number(data.tool_calls ?? current.tool_calls),
              tool_failures: Number(data.tool_failures ?? current.tool_failures),
            }))
            return
          }
          case 'approval.requested': {
            const ask = data.ask as AskRequest | null | undefined
            const requested: ApprovalRequestedData = ask
              ? {
                  kind: 'ask',
                  approval_id: String(data.approval_id ?? ''),
                  ask,
                  request_id: String(data.request_id ?? ''),
                }
              : {
                  kind: 'approval',
                  approval_id: String(data.approval_id ?? ''),
                  tool_name: String(data.tool_name ?? ''),
                  level: String(data.level ?? ''),
                  arguments: (data.arguments as Record<string, unknown>) ?? {},
                  timeout: Number(data.timeout ?? 0),
                  request_id: String(data.request_id ?? ''),
                }
            setApproval(requested)
            setAudit((current) => ({ ...current, approvals: current.approvals + 1 }))
            return
          }
          case 'approval.resolved': {
            setApproval((current) => {
              const resolvedId = String(data.approval_id ?? '')
              if (current && resolvedId && current.approval_id !== resolvedId) return current
              return null
            })
            return
          }
          case 'session.status': {
            const status = String(data.status ?? '')
            const current = sessionRef.current
            if (current) {
              const next: Session = { ...current, status: (status || current.status) as Session['status'] }
              setSession(next)
              sessionRef.current = next
              updateRef.current(next)
            }
            if (status && status !== 'waiting_approval') setApproval(null)
            // 计划审阅只在运行期间有效：轮次结束（完成 / 失败 / 取消）后必须清掉，
            // 否则取消路径不会发出 plan.review_resolved，卡片会一直挂着。
            if (status && status !== 'running') setPlanReview(null)
            return
          }
          case 'session.usage': {
            const prompt = Number(data.prompt_tokens ?? 0)
            const completion = Number(data.completion_tokens ?? 0)
            const total = Number(data.total_tokens ?? prompt + completion)
            setUsage((current) => ({
              prompt: current.prompt + prompt,
              completion: current.completion + completion,
              total: current.total + total,
            }))
            const current = sessionRef.current
            if (current) {
              const next: Session = {
                ...current,
                prompt_tokens: current.prompt_tokens + prompt,
                completion_tokens: current.completion_tokens + completion,
                total_tokens: current.total_tokens + total,
              }
              setSession(next)
              sessionRef.current = next
              updateRef.current(next)
            }
            return
          }
          case 'memory.updated': {
            setMemoryNotice({
              kind: String(data.kind ?? ''),
              message: String(data.message ?? ''),
            })
            // /save、/memory delete 等改了长期记忆：条目列表重拉一次
            // （上下文压缩类的通知也会走到这里，多拉一次只读端点无妨）。
            refreshMemoryRef.current()
            return
          }
          case 'plan.updated': {
            const payload = data as unknown as PlanPayload
            setItems((current) => upsertTaskCard(current, 'plan', payload))
            return
          }
          case 'team.updated': {
            const payload = data as unknown as TeamPayload
            setItems((current) => upsertTaskCard(current, 'team', payload))
            return
          }
          case 'router.updated': {
            // 普通对话轮在开关开启时按复杂度换档，这里回显本轮实际使用的档位
            setRouter({
              enabled: Boolean(data.enabled),
              tier: String(data.tier ?? ''),
              tier_idx: Number(data.tier_idx ?? 0),
              provider: String(data.provider ?? ''),
              model: String(data.model ?? ''),
              configured: Boolean(data.configured),
              confidence: Number(data.confidence ?? 0),
              hard_rule: Boolean(data.hard_rule),
              ...(data.error ? { error: String(data.error) } : {}),
            })
            return
          }
          case 'plan.review': {
            // `/plan`、`/team` 生成计划后阻塞等待审阅：批准前不会执行任何工具。
            setPlanReview({
              review_id: String(data.review_id ?? ''),
              mode: data.mode === 'team' ? 'team' : 'plan',
              plan: (data.plan as PlanReviewRequest['plan']) ?? null,
              timeout: Number(data.timeout ?? 0),
            })
            return
          }
          case 'plan.review_resolved': {
            setPlanReview((current) => {
              const resolvedId = String(data.review_id ?? '')
              if (current && resolvedId && current.review_id !== resolvedId) return current
              return null
            })
            return
          }
          case 'error': {
            setError(String(data.message ?? event.message ?? '发生未知错误'))
            return
          }
          default:
            return
        }
    }

    const socket = new SessionSocket(projectId, sessionId, {
      onState: setConnection,
      onEvent: applyEvent,
    })
    socketRef.current = socket
    socket.open()
    return () => {
      // 攒批里可能还压着内容，切会话/卸载时必须丢掉并取消定时器。
      batch.reset()
      socket.close()
      socketRef.current = null
    }
  }, [projectId, sessionId])

  const sendMessage = useCallback((content: string) => {
    const text = content.trim()
    if (!text) return
    setError(null)
    socketRef.current?.send({ type: 'user_message', request_id: newRequestId('msg'), content: text })
  }, [])

  const cancel = useCallback(() => {
    socketRef.current?.send({ type: 'cancel', request_id: newRequestId('cancel') })
  }, [])

  const resolveApproval = useCallback<SessionTimelineValue['resolveApproval']>((decision, options) => {
    const approvalId = approval?.approval_id ?? ''
    socketRef.current?.send({
      type: decision,
      request_id: newRequestId(decision),
      approval_id: approvalId,
      ...(options?.args ? { args: options.args } : {}),
      ...(options?.scope ? { scope: options.scope } : {}),
    })
    setApproval(null)
  }, [approval])

  const answerAsk = useCallback(
    (answers: Record<string, string> | null) => {
      const approvalId = approval?.approval_id ?? ''
      if (answers === null) {
        socketRef.current?.send({
          type: 'ask_cancel',
          request_id: newRequestId('ask'),
          approval_id: approvalId,
        })
      } else {
        socketRef.current?.send({
          type: 'ask_answer',
          request_id: newRequestId('ask'),
          approval_id: approvalId,
          answers,
        })
      }
      setApproval(null)
    },
    [approval],
  )

  const resolvePlanReview = useCallback<SessionTimelineValue['resolvePlanReview']>(
    (action, feedback) => {
      const reviewId = planReview?.review_id ?? ''
      socketRef.current?.send({
        type: 'plan_decision',
        request_id: newRequestId('review'),
        review_id: reviewId,
        action,
        ...(feedback ? { feedback } : {}),
      })
      setPlanReview(null)
    },
    [planReview],
  )

  const clearError = useCallback(() => setError(null), [])

  const visibleItems = useMemo<TimelineItem[]>(() => {
    const extra: TimelineItem[] = []
    if (stream.thinking) {
      extra.push({ kind: 'thinking', id: STREAM_THINK_ID, content: stream.thinking })
    }
    if (stream.content) {
      extra.push({ kind: 'agent', id: STREAM_AGENT_ID, content: stream.content, at: '', streaming: true })
    }
    return extra.length ? [...items, ...extra] : items
  }, [items, stream])

  return {
    items: visibleItems,
    stream,
    connection,
    audit,
    usage,
    session,
    approval,
    planReview,
    router,
    memory,
    memoryNotice,
    hitl,
    error,
    refreshMemory,
    sendMessage,
    cancel,
    resolveApproval,
    answerAsk,
    resolvePlanReview,
    clearError,
  }
}

function upsertTaskCard(
  items: TimelineItem[],
  kind: 'plan' | 'team',
  payload: PlanPayload | TeamPayload,
): TimelineItem[] {
  const isStart =
    kind === 'plan'
      ? payload.kind === 'plan_generated'
      : payload.kind === 'team_started' || payload.kind === 'team_plan_generated'

  if (isStart) {
    const id = `${kind}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`
    return [...items, { kind, id, payload } as TimelineItem]
  }

  let lastIndex = -1
  for (let i = items.length - 1; i >= 0; i -= 1) {
    if (items[i].kind === kind) {
      lastIndex = i
      break
    }
  }
  if (lastIndex === -1) {
    const id = `${kind}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`
    return [...items, { kind, id, payload } as TimelineItem]
  }
  const next = items.slice()
  next[lastIndex] = { kind, id: next[lastIndex].id, payload } as TimelineItem
  return next
}
