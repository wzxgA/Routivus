import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { newRequestId } from '../api/client'
import type {
  ApprovalRequestedData,
  AskRequest,
  Message,
  PlanPayload,
  PlanReviewRequest,
  RouterState,
  Session,
  SessionSnapshot,
  TeamPayload,
  WsEnvelope,
} from '../api/types'
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
  memoryNotice: { kind: string; message: string } | null
  hitl: string | null
  error: string | null
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
      return {
        kind: 'tool',
        id: message.id,
        name: message.tool_name ?? 'tool',
        args: '',
        ok: null,
        output: message.tool_result ?? message.content,
        error: '',
        durationMs: null,
        at: message.created_at,
      }
    case 'system':
      return { kind: 'system', id: message.id, content: message.content, at: message.created_at }
    default:
      return null
  }
}

/**
 * 会话事件流：把 WebSocket 事件归约为可渲染的时间线。
 *
 * 断线恢复策略：每次（重）连服务端都会推送完整 `session.snapshot`（含持久化消息），
 * 前端以快照为准重建时间线，再按 `sequence` 严格递增应用增量事件。窗口内的
 * 计划/团队卡属于瞬时状态，不在消息表内，重连后不保证复原。
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
  const [memoryNotice, setMemoryNotice] = useState<{ kind: string; message: string } | null>(null)
  const [hitl, setHitl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const socketRef = useRef<SessionSocket | null>(null)
  const sessionRef = useRef<Session | null>(initialSession)
  const updateRef = useRef(onSessionUpdate)

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

    // 切换会话：清空上一会话的时间线，避免串数据。
    setItems([])
    setStream({ content: '', thinking: '' })
    setApproval(null)
    setPlanReview(null)
    setRouter(null)
    setMemoryNotice(null)
    setError(null)

    const socket = new SessionSocket(projectId, sessionId, {
      onState: setConnection,
      onEvent: (event: WsEnvelope) => {
        const data = (event.data ?? {}) as Record<string, unknown>
        switch (event.type) {
          case 'session.snapshot': {
            const snapshot = data as unknown as SessionSnapshot
            const restored = (snapshot.messages ?? [])
              .map(messageToItem)
              .filter((item): item is TimelineItem => item !== null)
            setItems(restored)
            setStream({ content: '', thinking: '' })
            setAudit(snapshot.audit ?? { tool_calls: 0, tool_failures: 0, approvals: 0 })
            setHitl(snapshot.safety?.hitl ?? null)
            setRouter(snapshot.router ?? null)
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
            return
          }
          case 'message.created': {
            const message = data.message as Message | undefined
            if (!message) return
            setItems((current) => [...current, { kind: 'user', id: message.id, content: message.content, at: message.created_at }])
            return
          }
          case 'message.delta': {
            const kind = String(data.kind ?? 'content')
            const text = String(data.text ?? '')
            if (!text) return
            setStream((current) =>
              kind === 'thinking'
                ? { ...current, thinking: current.thinking + text }
                : { ...current, content: current.content + text },
            )
            return
          }
          case 'message.completed': {
            const message = data.message as Message | undefined
            setStream({ content: '', thinking: '' })
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
      },
    })
    socketRef.current = socket
    socket.open()
    return () => {
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
    memoryNotice,
    hitl,
    error,
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
