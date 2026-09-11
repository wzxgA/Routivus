// 与 routivus/server/schemas.py 一一对应的领域模型。
// 时间字段统一为 ISO 8601 字符串（服务端 jsonable_encoder 输出）。

export type ProjectStatus = 'idle' | 'working' | 'error' | 'archived'

export type SessionStatus =
  | 'idle'
  | 'running'
  | 'waiting_approval'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type MessageRole = 'user' | 'assistant' | 'tool' | 'system'

export interface ProjectStats {
  sessions: number
  notes: number
  calls_today: number
}

export interface Project {
  id: string
  name: string
  root_path: string
  branch: string | null
  status: ProjectStatus
  created_at: string
  updated_at: string
  stats: ProjectStats
}

export interface Session {
  id: string
  project_id: string
  title: string
  status: SessionStatus
  active_provider: string
  active_model: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  created_at: string
  updated_at: string
}

export interface Message {
  id: string
  session_id: string
  role: MessageRole
  content: string
  tool_name: string | null
  tool_args: Record<string, unknown> | null
  tool_result: string | null
  created_at: string
}

export interface Note {
  id: string
  project_id: string | null
  project_name: string | null
  title: string
  body_markdown: string
  preview: string
  tags: string[]
  pinned: boolean
  created_at: string
  updated_at: string
  version: number
}

export interface ActivityDay {
  date: string
  count: number
  projects: Record<string, number>
}

export interface NoteStats {
  total: number
  global_notes: number
  project_notes: number
  by_project: Record<string, number>
}

// ---- Web Console 配置接口 ----

export interface ProviderView {
  name: string
  display_name: string | null
  api_base: string
  default_model: string
  models: string[]
  has_key: boolean
  api_key_masked: string
  is_base: boolean
  layer: string
}

export interface TierView {
  name: string
  provider: string
  model: string
  configured: boolean
}

export interface ConfigSnapshot {
  active_provider: string
  active_model: string
  providers: ProviderView[]
  tiers: TierView[]
  smart_router_enabled: boolean
  user_dir: string
  legacy_user_dir: string | null
  desktop: boolean
}

export interface DesktopInfo {
  desktop: boolean
  user_dir: string
  legacy_user_dir: string | null
  static_dir: string | null
  allowed_roots: string[]
}

// ---- WebSocket 事件 ----

export interface WsEnvelope<T = unknown> {
  type: string
  event_id?: string
  sequence?: number
  session_id?: string
  project_id?: string
  occurred_at?: string
  data?: T
  // 少数事件（error / ping / pong）把字段放在顶层
  code?: string
  message?: string
  request_id?: string
  terminal_id?: string
}

export interface SessionSnapshot {
  project: { id: string; name: string; root_path: string }
  session: Session
  messages: Message[]
  memory: { project_id: string; scope: string; status: string; items: unknown[] }
  safety: { project_id: string; hitl: string; status: string }
  router?: RouterState
  audit: { tool_calls: number; tool_failures: number; approvals: number }
  /**
   * 卡片类历史事件（plan/team/tool/command/router）：结构化状态不落消息表，
   * 重连时由前端按序回放给同一套 reducer，用来重建卡片渲染。
   */
  replay?: ReplayEvent[]
  /** 仍挂起的交互（内存桥）：恢复成可继续应答的审批 / 计划审阅卡。 */
  pending?: {
    approval?: ApprovalRequestedData
    plan_review?: PlanReviewRequest
  }
  last_sequence: number
}

export interface ReplayEvent {
  type: string
  sequence: number
  data: Record<string, unknown>
}

/**
 * 智能路由状态（服务端 `router.updated` 事件 / 会话快照 `router` 字段）。
 *
 * 普通对话轮在开关开启时会先按任务复杂度路由，再把 `agent.llm` 换到结果档；
 * `tier` / `provider` / `model` 即本轮实际使用的档位。路由或换模型失败时
 * `error` 非空，该轮沿用原模型。
 */
export interface RouterState {
  enabled: boolean
  tier?: string
  tier_idx?: number
  provider?: string
  model?: string
  configured?: boolean
  confidence?: number
  hard_rule?: boolean
  error?: string
}

/** Skill 元数据（GET /api/skills）；正文由会话内 load_skill 工具按需加载。 */
export interface SkillView {
  name: string
  description: string
  source: string
  version?: string | null
  enabled: boolean
  valid: boolean
  error: string
}

/** 一条命令补全候选（与 GET /api/completions 的 candidates 项对应）。 */
export interface CompletionCandidate {
  label: string
  insert_text: string
  detail: string
  kind: string
}

/** 补全响应：replace_start/end 是应用候选时 client 端替换的字符区间。 */
export interface CompletionResponse {
  is_command: boolean
  replace_start: number
  replace_end: number
  candidates: CompletionCandidate[]
}

export interface ToolStartedData {
  tool_call_id: string
  name: string
  arguments: string
  request_id?: string
}

export interface ToolCompletedData {
  tool_call_id: string
  name: string
  ok: boolean
  output: string
  error: string
  duration_ms: number
  request_id?: string
}

export interface MessageDeltaData {
  kind: 'content' | 'thinking' | string
  text: string
  request_id?: string
}

export interface PlanTask {
  id: string
  title: string
  description: string
  deps: string[]
  status: 'pending' | 'running' | 'done' | 'failed'
  result: string
}

export interface PlanPayload {
  kind: string
  message?: string
  plan?: { goal: string; tasks: PlanTask[]; batches: string[][] } | null
  batch?: string[]
  task?: PlanTask | null
}

export interface TeamTask {
  id: string
  title: string
  description: string
  deps: string[]
  owner_role: string
  status?: string
  acceptance_criteria?: string[]
}

export interface TeamPayload {
  kind: string
  team_id?: string
  message?: string
  plan?: { goal: string; tasks: TeamTask[]; batches: string[][] } | null
  batch?: string[]
  task?: TeamTask | null
  agent_id?: string
  role?: string
  attempt?: number
  failure_category?: string
}

/**
 * 计划 / 团队审阅请求（服务端 `plan.review` 事件）。
 *
 * 由 `PlanReviewBridge` 发出：`/plan`、`/team` 生成计划后进入阻塞式审阅，
 * 客户端必须用 `plan_decision` 应答 execute / cancel / replan，超时按 cancel 落地。
 */
export interface PlanReviewTask extends PlanTask {
  owner_role?: string
}

export interface PlanReviewRequest {
  review_id: string
  mode: 'plan' | 'team'
  plan?: { goal: string; tasks: PlanReviewTask[]; batches: string[][] } | null
  timeout?: number
}

export interface AskField {
  name?: string
  label?: string
  type?: string
  required?: boolean
  options?: string[]
  placeholder?: string
}

export interface AskRequest {
  question?: string
  fields?: AskField[]
  [key: string]: unknown
}

/**
 * 终端通道事件。
 *
 * 注意：终端事件是**扁平结构**（字段全部在顶层），不走会话事件的 `data` 信封 ——
 * 见 `routivus/server/terminal.py` 的 `_send_output` 与 `app.py` 的 `terminal.*` 分支。
 */
export interface TerminalEnvelope {
  type: string
  terminal_id?: string
  request_id?: string
  code?: string
  message?: string
  occurred_at?: string
  // terminal.opened
  cwd?: string
  shell?: string | string[]
  backend?: string
  cols?: number
  rows?: number
  // terminal.output
  seq?: number
  data?: string
  encoding?: 'utf8' | 'base64'
  // terminal.output.dropped
  count?: number
  // terminal.closed
  reason?: string
  exit_code?: number | null
}

export interface ApprovalRequestedData {
  kind?: 'approval' | 'ask'
  approval_id?: string
  tool_name?: string
  level?: string
  arguments?: Record<string, unknown>
  timeout?: number
  ask?: AskRequest | null
  request_id?: string
}

export interface ApprovalResolvedData {
  kind?: 'approval' | 'ask'
  approval_id?: string
  tool_name?: string
  decision?: string
  reason?: string
  modified?: boolean
  request_id?: string
}
