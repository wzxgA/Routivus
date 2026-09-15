import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { fetchSessionMemory } from '../api'
import { newRequestId } from '../api/client'
import type {
  ApprovalRequestedData,
  AskRequest,
  ContextPayload,
  MemoryPayload,
  Message,
  PlanPayload,
  PlanReviewRequest,
  ReplayEvent,
  RouterState,
  Session,
  SessionSnapshot,
  TeamPayload,
  WsEnvelope,
} from '../api/types'
import { routerNoticeView, type RouterNoticeView } from '../utils/routerNotes'
import { StreamBatcher, type StreamDelta } from '../utils/streamBatch'
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
  /** 子任务来源（task:<id> / agent:<id>）；主会话条目无（方案 07 §4.10）。 */
  source?: string
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

/**
 * 对话流里的换档卡（方案 14 §4.5）：结构化载荷由 `routerNoticeView` 生成，
 * 触发判据与旧的文字提示一致（只在 变化 / 回落 / 失败 / 冻结 / 防降级 时出现）。
 * `fresh` 标记"在线事件插入"——回放重建的条目没有它，卡片据此走静态终态
 * （刷新 / 重连后不重播动画）。
 */
export interface RouterItem {
  kind: 'router'
  id: string
  view: RouterNoticeView
  at: string
  fresh?: boolean
}

export type TimelineItem =
  | { kind: 'user'; id: string; content: string; at: string }
  | { kind: 'agent'; id: string; content: string; at: string; streaming?: boolean; source?: string }
  | { kind: 'thinking'; id: string; content: string; streaming?: boolean; source?: string }
  | { kind: 'system'; id: string; content: string; at: string }
  | { kind: 'command'; id: string; command: string; content: string; ok: boolean; at: string }
  | ToolItem
  | PlanItem
  | TeamItem
  | RouterItem

export type ThinkingItem = Extract<TimelineItem, { kind: 'thinking' }>
export type AgentItem = Extract<TimelineItem, { kind: 'agent' }>

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

/**
 * 流式中的"活跃段"（按来源分桶，方案 07 §4.2/§4.8）。
 *
 * 后端在段边界（kind 切换 / 工具调用 / 轮次结束）落库并广播 `message.segment`，
 * 前端保持同样的分界：kind 切换即视为段边界，避免两套逻辑漂移。
 * 键为来源（"" = 主 ReAct 轮，task:<id> / agent:<id> = 子任务）。
 */
type StreamSegment = { kind: 'content' | 'thinking'; text: string }

export interface SessionTimelineValue {
  items: TimelineItem[]
  connection: ConnState
  audit: AuditTotals
  usage: UsageTotals
  session: Session | null
  approval: ApprovalRequestedData | null
  planReview: PlanReviewRequest | null
  router: RouterState | null
  /**
   * 路由中（方案 14 §4.3）：普通消息已发出、`router.updated` 未返回的瞬态。
   * 进入 = sendMessage（非斜杠输入且开关开启）；三条回落 = ① router.updated、
   * ② 证明本轮不路由的事件（plan / team / 命令 / 首个 delta 等）、③ 2s 兜底超时。
   */
  routerRouting: boolean
  memory: MemoryPayload | null
  /**
   * 当前模型的窗口 / 输出上限（快照恢复 + `context.updated` 更新）。
   *
   * **会变**：`/model` 换模型与 SmartRouter 换档都会让它变化，所以界面上的
   * 使用率分母与输出上限都必须读它，不能缓存成常量。
   */
  context: ContextPayload | null
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

function messageToItem(message: Message, source?: string): TimelineItem | null {
  const withSource = source ? { source } : {}
  switch (message.role) {
    case 'user':
      return { kind: 'user', id: message.id, content: message.content, at: message.created_at }
    case 'assistant':
      return { kind: 'agent', id: message.id, content: message.content, at: message.created_at, ...withSource }
    case 'thinking':
      // 思考段落库为独立消息：回看默认折叠（ThinkingBlock 的组件内初值），
      // 刷新 / 重连后不再刷屏。来源随 message.segment 在线携带，重连后不可得。
      return { kind: 'thinking', id: message.id, content: message.content, ...withSource }
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

// ---- 条目归约的纯函数（在线事件与快照回放共用，避免两套逻辑漂移）----------------

/** `router.updated` 载荷 → RouterState（在线增量与快照回放共用同一份解析）。 */
function parseRouterState(data: Record<string, unknown>): RouterState {
  return {
    enabled: Boolean(data.enabled),
    tier: String(data.tier ?? ''),
    tier_idx: Number(data.tier_idx ?? 0),
    provider: String(data.provider ?? ''),
    model: String(data.model ?? ''),
    configured: Boolean(data.configured),
    confidence: Number(data.confidence ?? 0),
    hard_rule: Boolean(data.hard_rule),
    score: typeof data.score === 'number' ? Number(data.score) : undefined,
    notes: Array.isArray(data.notes) ? (data.notes as unknown[]).map(String) : undefined,
    switched: typeof data.switched === 'boolean' ? Boolean(data.switched) : undefined,
    elapsed_ms: typeof data.elapsed_ms === 'number' ? Number(data.elapsed_ms) : undefined,
    load_ms: typeof data.load_ms === 'number' ? Number(data.load_ms) : undefined,
    ...(data.error ? { error: String(data.error) } : {}),
  }
}

function withMessage(items: TimelineItem[], message: Message): TimelineItem[] {
  const item = messageToItem(message)
  if (!item) return items
  return [...items, item]
}

function withCommand(items: TimelineItem[], data: Record<string, unknown>): TimelineItem[] {
  // 把回执条目标记为命令结果（ok 决定成败配色）。优先按 message_id
  // 精确定位——回放时"最近一条 agent"的启发式不再可靠。
  const command = String(data.command ?? '')
  const ok = Boolean(data.ok)
  const messageId = String(data.message_id ?? '')
  let index = -1
  if (messageId) {
    index = items.findIndex((item) => item.id === messageId)
  } else {
    for (let cursor = items.length - 1; cursor >= 0; cursor -= 1) {
      if (items[cursor]?.kind === 'agent') {
        index = cursor
        break
      }
    }
  }
  const item = index >= 0 ? items[index] : undefined
  if (!item || item.kind !== 'agent') return items
  const next = [...items]
  next[index] = { kind: 'command', id: item.id, command, content: item.content, ok, at: item.at }
  return next
}

function withToolStarted(items: TimelineItem[], data: Record<string, unknown>): TimelineItem[] {
  const toolCallId = String(data.tool_call_id ?? '')
  const source = String(data.source ?? '')
  return [
    ...items,
    {
      kind: 'tool',
      id: toolCallId || `tool-${items.length}`,
      name: String(data.name ?? 'tool'),
      args: String(data.arguments ?? ''),
      ok: null,
      output: '',
      error: '',
      durationMs: null,
      at: new Date().toISOString(),
      ...(source ? { source } : {}),
    },
  ]
}

function withToolCompleted(items: TimelineItem[], data: Record<string, unknown>): TimelineItem[] {
  const toolCallId = String(data.tool_call_id ?? '')
  return items.map((item) =>
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
  )
}

/** 卡片类事件对 items 的归约；快照回放与在线事件共用。 */
function withCardEvent(items: TimelineItem[], type: string, data: Record<string, unknown>): TimelineItem[] {
  switch (type) {
    case 'tool.started':
      return withToolStarted(items, data)
    case 'tool.completed':
      return withToolCompleted(items, data)
    case 'plan.updated':
      return upsertTaskCard(items, 'plan', data as unknown as PlanPayload)
    case 'team.updated':
      return upsertTaskCard(items, 'team', data as unknown as TeamPayload)
    case 'command.executed':
      return withCommand(items, data)
    default:
      return items
  }
}

/**
 * 会话事件流：把 WebSocket 事件归约为可渲染的时间线。
 *
 * 断线恢复策略：每次（重）连服务端都会推送完整 `session.snapshot`，前端以快照
 * 为准重建——文本条目来自 `snapshot.messages`，卡片来自 `snapshot.replay` 的
 * 历史事件，两者按 `created_at` / `occurred_at`（同源时钟）**归并成一条时间线**
 * 后依次回放给同一套 reducer（方案 07 §4.6a：重连后的顺序 = 在线顺序）。
 * 仍挂起的交互由 `snapshot.pending` 恢复成可继续应答的卡片；此后按 `sequence`
 * 严格递增应用增量事件。工具卡只认事件（消息表里的 tool 角色缺少参数/成败/耗时）。
 */
export function useSessionTimeline(
  projectId: string | null,
  sessionId: string | null,
  initialSession: Session | null,
  onSessionUpdate: (session: Session) => void,
): SessionTimelineValue {
  const [items, setItems] = useState<TimelineItem[]>([])
  const [segments, setSegments] = useState<Record<string, StreamSegment>>({})
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
  const [context, setContext] = useState<ContextPayload | null>(null)
  const [memoryNotice, setMemoryNotice] = useState<{ kind: string; message: string } | null>(null)
  const [hitl, setHitl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const socketRef = useRef<SessionSocket | null>(null)
  const sessionRef = useRef<Session | null>(initialSession)
  const updateRef = useRef(onSessionUpdate)
  // 上一轮路由状态：用于判断"档位是否变化"（方案 08 §4.4 的换档提示）。
  // 放在 ref 里而不是依赖 state，避免在 setState 的 updater 里做副作用。
  const routerRef = useRef<RouterState | null>(null)

  // 「路由中」瞬态（方案 14 §4.3）：进入在 sendMessage，回落有三条
  // （router.updated / 证明不路由的事件 / 2s 兜底超时），防 /plan 等轮次把 chip 挂死。
  const [routerRouting, setRouterRouting] = useState(false)
  const routerRoutingTimer = useRef<number | null>(null)

  const clearRouterRouting = useCallback(() => {
    if (routerRoutingTimer.current !== null) {
      window.clearTimeout(routerRoutingTimer.current)
      routerRoutingTimer.current = null
    }
    setRouterRouting(false)
  }, [])

  const beginRouterRouting = useCallback(() => {
    if (routerRoutingTimer.current !== null) window.clearTimeout(routerRoutingTimer.current)
    // 兜底（③）：正常路由 <500ms；2s 内没有回落事件说明这轮根本不路由，收回状态。
    routerRoutingTimer.current = window.setTimeout(() => {
      routerRoutingTimer.current = null
      setRouterRouting(false)
    }, 2000)
    setRouterRouting(true)
  }, [])

  // socket effect 里要调 clearRouterRouting 但不能把它加进依赖（会导致重连），
  // 照 updateRef / refreshMemoryRef 的做法走 ref。
  const clearRouterRoutingRef = useRef(clearRouterRouting)
  useEffect(() => {
    clearRouterRoutingRef.current = clearRouterRouting
  }, [clearRouterRouting])

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
    const batch = new StreamBatcher((delta: StreamDelta) => {
      setSegments((current) => {
        const previous = current[delta.source]
        // 来源相同且 kind 相同才续写；否则从头开段（旧段已由 message.segment 收走）
        const base = previous && previous.kind === delta.kind ? previous.text : ''
        return { ...current, [delta.source]: { kind: delta.kind, text: base + delta.text } }
      })
    })

    /** 清空全部活跃段：连同攒批里的尾巴一起丢掉（否则定时器到点会把旧内容补回来）。 */
    const clearSegments = () => {
      batch.reset()
      setSegments({})
    }

    // 切换会话：清空上一会话的时间线，避免串数据。
    setItems([])
    clearSegments()
    setApproval(null)
    setPlanReview(null)
    setRouter(null)
    routerRef.current = null
    clearRouterRoutingRef.current()
    let routerNoticeSeq = 0
    setMemory(null)
    setContext(null)
    setMemoryNotice(null)
    setError(null)

    // 事件归约：在线增量与快照回放共用同一套 reducer，避免两套渲染逻辑漂移。
    const applyEvent = (event: WsEnvelope) => {
        const data = (event.data ?? {}) as Record<string, unknown>
        // 「路由中」回落（②，方案 14 §4.3）：这些事件证明本轮不走路由（/plan、/team、
        // 斜杠命令）或路由阶段已经过去。router.updated 走自己 case 里的回落（①）。
        // 注意**不**包含 message.created 与 session.status=running——它们在路由前到达，
        // 清早了会让 shimmer 一闪而过。
        switch (event.type) {
          case 'message.delta':
          case 'tool.started':
          case 'plan.updated':
          case 'plan.review':
          case 'team.updated':
          case 'command.executed':
          case 'error':
            clearRouterRoutingRef.current()
            break
          default:
            break
        }
        switch (event.type) {
          case 'session.snapshot': {
            const snapshot = data as unknown as SessionSnapshot
            // 时间戳归并（方案 07 §4.6a）：messages 与 replay 同源时钟
            // （storage._now()），合一条按时间排序的序列后依次重建。
            // 同一时间戳时消息在前（段落先落库、后发事件），order 作次级键保证稳定。
            const messages = snapshot.messages ?? []
            type RestoreEntry = { at: string; order: number; item?: TimelineItem; event?: WsEnvelope }
            const entries: RestoreEntry[] = []
            messages.forEach((message, index) => {
              const item = messageToItem(message)
              if (item) entries.push({ at: message.created_at, order: index, item })
            })
            ;(snapshot.replay ?? []).forEach((replayed: ReplayEvent, index) => {
              entries.push({
                at: String(replayed.occurred_at ?? ''),
                order: messages.length + index,
                event: { type: replayed.type, sequence: replayed.sequence, data: replayed.data } as WsEnvelope,
              })
            })
            entries.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : a.order - b.order))
            let restored: TimelineItem[] = []
            for (const entry of entries) {
              if (entry.item) {
                restored = [...restored, entry.item]
              } else if (entry.event) {
                restored = withCardEvent(
                  restored,
                  entry.event.type,
                  (entry.event.data ?? {}) as Record<string, unknown>,
                )
                // 换档卡在回放里也要在原位重建（方案 14 §4.5）。此前 router.updated 走
                // applyEvent，它插进 items 的提示会被循环末尾的 setItems(restored) 整体
                // 覆盖——刷新 / 重连后换档提示静默丢失。这里直接并入 restored（顺序已按
                // 时间戳归并），且不带 fresh 标记：回放是冷渲染，不重播动画。
                if (entry.event.type === 'router.updated') {
                  const next = parseRouterState((entry.event.data ?? {}) as Record<string, unknown>)
                  const view = routerNoticeView(routerRef.current, next)
                  if (view) {
                    restored = [
                      ...restored,
                      {
                        kind: 'router',
                        id: `router-${entry.event.sequence ?? `local-${(routerNoticeSeq += 1)}`}`,
                        view,
                        at: entry.at,
                      },
                    ]
                  }
                  routerRef.current = next
                }
              }
            }
            setItems(restored)
            clearSegments()
            setAudit(snapshot.audit ?? { tool_calls: 0, tool_failures: 0, approvals: 0 })
            setHitl(snapshot.safety?.hitl ?? null)
            setRouter(snapshot.router ?? null)
            // 同步"上一轮档位"：重连后的第一条 router.updated 不该被当成首轮提示
            routerRef.current = snapshot.router ?? null
            // 项目长期记忆条目随快照下发，重连/切会话即可见。
            setMemory(snapshot.memory ?? null)
            // 当前模型的窗口 / 输出上限：重连即可见，不必等下一轮对话刷新。
            setContext(snapshot.context ?? null)
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
            return
          }
          case 'message.created': {
            const message = data.message as Message | undefined
            if (!message) return
            // 命令回执也是 assistant 消息（run_command_turn 落库后广播），
            // 不能再像用户输入那样渲染成右侧气泡。
            setItems((current) => withMessage(current, message))
            return
          }
          case 'message.segment': {
            const message = data.message as Message | undefined
            const source = String(data.source ?? '')
            // 段落已完整落库：丢掉该来源攒批里的尾巴，避免重复补上。
            batch.reset(source)
            setSegments((current) => {
              if (!(source in current)) return current
              const next = { ...current }
              delete next[source]
              return next
            })
            if (message) {
              const item = messageToItem(message, source || undefined)
              if (item) setItems((current) => [...current, item])
            }
            return
          }
          case 'command.executed': {
            setItems((current) => withCommand(current, data))
            return
          }
          case 'message.delta': {
            batch.push(String(data.source ?? ''), String(data.kind ?? 'content'), String(data.text ?? ''))
            return
          }
          case 'message.completed': {
            // 正文 / 思考已由 message.segment 逐段接管（方案 07 §4.3）：
            // 这条事件只表示"本轮结束"，负责清掉活跃段。
            clearSegments()
            return
          }
          case 'tool.started': {
            setItems((current) => withToolStarted(current, data))
            return
          }
          case 'tool.completed': {
            setItems((current) => withToolCompleted(current, data))
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
                  timeout: Number(data.timeout ?? 0),
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
            // 轮次以完成 / 失败 / 取消 / 空闲收尾时，不会再有任何 message.segment 或
            // message.completed 来收活跃段——这里兜底清掉，否则取消后界面会永久
            // 留一个空的流式块（看起来就是一条横线，且像卡住不动）。
            if (status === 'completed' || status === 'failed' || status === 'cancelled' || status === 'idle') {
              clearSegments()
              // 轮次收尾（含取消）：路由中的瞬态不该跨轮次留着
              clearRouterRoutingRef.current()
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
          case 'context.updated': {
            // 换模型（/model）或 SmartRouter 换档后，窗口与输出上限会变：
            // 用它更新使用率分母，不做任何换算，避免界面与后端算的不是同一个数。
            setContext({
              window: Number(data.window ?? 0),
              max_output: Number(data.max_output ?? 0),
              output_field: String(data.output_field ?? ''),
              provider: String(data.provider ?? ''),
              model: String(data.model ?? ''),
              source: String(data.source ?? ''),
            })
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
            // 普通对话轮在开关开启时按复杂度换档，这里回显本轮实际使用的档位。
            // 方案 08：带上判定依据（notes）与运行态（是否真换了模型 / 耗时）；
            // 方案 14 §4.5：变化 / 回落 / 失败 / 被稳定层拦住时插换档卡（结构化）。
            clearRouterRoutingRef.current()
            const next = parseRouterState(data)
            // 用 ref 记上一轮：不能放在 setRouter 的 updater 里算（StrictMode 会
            // 重复调用 updater，导致提示插入两次）。
            const view = routerNoticeView(routerRef.current, next)
            routerRef.current = next
            setRouter(next)
            if (view) {
              const id = `router-${event.sequence ?? `local-${(routerNoticeSeq += 1)}`}`
              setItems((current) => [...current, { kind: 'router', id, view, at: '', fresh: true }])
            }
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
    // 「路由中」进入（方案 14 §4.3）：只对普通消息置位——斜杠开头的是命令 / /plan /
    // /team，它们不参与路由；开关没开时也没有路由这一跳。
    if (!text.startsWith('/') && routerRef.current?.enabled) beginRouterRouting()
    socketRef.current?.send({ type: 'user_message', request_id: newRequestId('msg'), content: text })
  }, [beginRouterRouting])

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

  // 活跃段始终是末尾元素：已完成段都在 items 里且按时序 push，末尾追加即正确顺序。
  // 性能约定：段完成即变成 items 里不动的对象，ChatRow 的 memo 继续冻结历史行。
  const visibleItems = useMemo<TimelineItem[]>(() => {
    const extra: TimelineItem[] = []
    for (const [source, segment] of Object.entries(segments)) {
      if (!segment.text) continue
      const suffix = source || 'main'
      const withSource = source ? { source } : {}
      extra.push(
        segment.kind === 'thinking'
          ? { kind: 'thinking', id: `${STREAM_THINK_ID}-${suffix}`, content: segment.text, streaming: true, ...withSource }
          : { kind: 'agent', id: `${STREAM_AGENT_ID}-${suffix}`, content: segment.text, at: '', streaming: true, ...withSource },
      )
    }
    return extra.length ? [...items, ...extra] : items
  }, [items, segments])

  return {
    items: visibleItems,
    connection,
    audit,
    usage,
    session,
    approval,
    planReview,
    router,
    routerRouting,
    memory,
    context,
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
