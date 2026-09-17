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

/** thinking：模型推理段（仅展示用，方案 07 §4.1；不参与 agent 上下文重建）。 */
export type MessageRole = 'user' | 'assistant' | 'tool' | 'system' | 'thinking'

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

/** 一个用量桶（方案 12）：三件套 + 轮次数。 */
export interface UsageBucket {
  prompt: number
  completion: number
  total: number
  turns: number
}

export interface UsageDay extends UsageBucket {
  date: string
}

export interface UsageProject {
  project_id: string
  /** 项目名由 app 层按注册表补上；未知项目为空串。 */
  name: string
  today: number
  range: number
  lifetime: number
  turns: number
}

export interface UsageTier {
  /** 档位名；本版本之前的事件没有归因字段，统一记作「未标注」。 */
  tier: string
  total: number
  turns: number
}

/**
 * `/api/usage/summary` 的响应（方案 12）。
 *
 * **两条口径刻意分开**：`daily` / `range` / `by_tier` 来自逐轮 `session.usage` 事件，
 * `lifetime` 来自 sessions 的 token 快照求和。两者差异见 `drift`。
 */
export interface UsageSummary {
  /** 聚合用的时区（"今日"按它算）。 */
  timezone: string
  generated_at: string
  days: number
  today: UsageBucket
  range: UsageBucket
  lifetime: UsageBucket & { sessions: number }
  daily: UsageDay[]
  by_project: UsageProject[]
  by_tier: UsageTier[]
  drift: { events_total: number; sessions_total: number; diff: number; diff_pct: number }
}

export interface NoteStats {
  total: number
  global_notes: number
  project_notes: number
  by_project: Record<string, number>
}

// ---- SmartRouter 数据看板（方案 13，GET /api/router/insights）----

/** 文件事实（产物 / 编码器都要一份，用来显示体积与时间戳）。 */
export interface RouterFileFacts {
  path: string
  present: boolean
  bytes: number
  mtime: string
}

export interface RouterArtifactView extends RouterFileFacts {
  /** semantic（语义版）/ nosem（无语义兜底）/ unavailable */
  source: string
  reason_code: string
  n_samples: number | null
  val_accuracy: number | null
  trained_at: string
  sem_dim: number
  head_dim: number
  eval_error: string
}

export interface RouterSemanticView extends RouterFileFacts {
  available: boolean
  reason_code: string
  dim: number
  calls: number
  avg_ms: number
}

export interface RouterStatusView {
  /** true = 读自已加载的共享资产（与 chat 同源）；false = 只按文件与依赖预判 */
  verified: boolean
  load_seconds: number
  artifact: RouterArtifactView
  semantic: RouterSemanticView
  prev_artifact: RouterFileFacts
}

export interface RouterDiagnosisView {
  level: 'ok' | 'warn' | 'error'
  code: string
  text: string
  hints: string[]
}

export interface RouterRecentTurn {
  ts: string
  tier: string
  confidence: number | null
  score: number | null
  hard_rule: boolean
  notes: string[]
  elapsed_ms: number | null
  switched: boolean
  error: string
}

export interface RouterRuntimeView {
  total: number
  routed: number
  errors: number
  events_since: string
  truncated: boolean
  by_tier: Record<string, number>
  hard_rule: number
  hard_rule_ratio: number
  confidence_buckets: Record<string, number>
  switched: number
  latency_ms: {
    load_p50: number | null
    load_p95: number | null
    route_p50: number | null
    route_p95: number | null
    route_max: number | null
  }
  error_groups: { text: string; count: number }[]
  recent: RouterRecentTurn[]
}

export interface RouterCalibrationView {
  degraded: boolean
  bias: Record<string, number>
  samples: Record<string, number>
  threshold_adjust: number
  total: number
}

export interface RouterRuleView {
  feature: string
  op: string
  value: number
  action: number
  confidence: number
  support: number
}

export interface RouterSamplesView {
  degraded: boolean
  feedback_lines: number
  available_samples: number
  weighted_total: number
  unique_texts: number
  by_tier: Record<string, number>
  by_signal: Record<string, { up: number; down: number }>
  build_stats: Record<string, number>
  new_7d: number
  new_30d: number
  sem_samples: { count: number; bytes: number; max: number; newest_ts: number; oldest_ts: number }
  semantic_head: { present: boolean; tiers: number; dim: number; train_acc: number | null }
}

export interface RouterGateView {
  key: string
  label: string
  satisfied: boolean
  detail: string
}

export interface RouterEvolveStateView {
  last_attempt_at: number
  last_success_at: number
  samples_since_success: number
  last_decision: string
  last_reason: string
  last_holdout_acc: number | null
  last_baseline_acc: number | null
  evolve_count: number
}

export interface RouterEvolveLogRow {
  trigger?: string
  ts?: number
  decision?: string
  reason?: string
  samples?: number
  holdout_acc?: number | null
  baseline_acc?: number | null
  n_train?: number
  n_holdout?: number
  artifact_bytes?: number | null
}

export interface RouterEvolutionView {
  state: RouterEvolveStateView
  auto_evolve: boolean
  thresholds: Record<string, number>
  cooldown_remaining_days: number
  gates: RouterGateView[]
  counts_by_tier: Record<string, number>
  total_samples: number
  new_since_success: number
  history: RouterEvolveLogRow[]
}

/** 门槛常量（服务端下发，前端只显示、不硬编码）。 */
export interface RouterLimits {
  calibration_min_samples: number
  max_bias: number
  max_threshold_adjust: number
  rule_min_support: number
  rule_min_precision: number
  rule_max_confidence: number
  rules_max: number
  evolve_min_per_tier: number
  evolve_min_total: number
  evolve_min_total_first: number
  evolve_min_new: number
  evolve_cooldown_days: number
  holdout_ratio: number
  sem_samples_max: number
  signals: Record<string, { upgrade: boolean; weight: number }>
}

export interface RouterInsights {
  generated_at: string
  timezone: string
  days: number
  limits: RouterLimits
  status: RouterStatusView
  diagnosis: RouterDiagnosisView
  runtime: RouterRuntimeView
  calibration: RouterCalibrationView
  rules: { degraded: boolean; items: RouterRuleView[] }
  samples: RouterSamplesView
  evolution: RouterEvolutionView
}

// ---- Web Console 配置接口 ----

/** 单个模型的能力覆盖（config.json 的 `model_limits.<model>`）。 */
export interface ModelLimit {
  window?: number | null
  max_output?: number | null
}

/** 输出上限的请求字段名；空串表示该 provider 不发送该字段。 */
export type MaxTokensField = 'max_tokens' | 'max_completion_tokens' | ''

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
  /** provider 默认窗口（token）；按模型的覆盖在 model_limits 里。 */
  context_window: number
  /** 按模型的覆盖表：只写例外，没配的模型走 provider 默认。 */
  model_limits: Record<string, ModelLimit>
  /** 单次输出上限；0 = 不限制（不下发 max_tokens）。 */
  max_output_tokens: number
  max_tokens_field: MaxTokensField | string
}

/**
 * 会话当前模型的能力上限（快照 `context` 段 / `context.updated` 事件）。
 *
 * 三个数同源、同时变：`window` 是使用率分母，`max_output` 是下发的输出上限，
 * `output_field` 是实际发出去的字段名（给用户对照网关文档自检用）。换模型或
 * SmartRouter 换档都会让它变化——**不是会话常量**。
 */
export interface ContextPayload {
  window: number
  max_output: number
  output_field: string
  provider: string
  model: string
  /** 窗口来自哪一层：env / model（模型覆盖）/ provider / default。 */
  source: string
}

export interface TierView {
  name: string
  provider: string
  model: string
  configured: boolean
  /**
   * 该档**实际会用**的 provider / model（未显式配置时即回落 active）。
   * 配置页据此显示"回落 active → 实际 base·m-base"（方案 08 §4.3）。
   */
  resolved_provider?: string
  resolved_model?: string
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
  /** 当前生效（已按 active_model 解析）的能力上限：取代前端构建期常量。 */
  context_window: number
  max_output_tokens: number
  max_tokens_field: MaxTokensField | string
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

/** 一条项目长期记忆（服务端 MemoryEntry 的视图）。 */
export interface MemoryItem {
  id: number
  content: string
  source: string
  created_at: string
  updated_at: string
}

/**
 * 项目长期记忆视图（会话快照的 `memory` 段 / `GET /api/sessions/{id}/memory`）。
 *
 * 记忆是**项目级**数据：同一项目的所有会话共享同一份；`status` 为
 * `unavailable` 表示库读不到（此时 items 为空、error 有说明）。
 */
export interface MemoryPayload {
  project_id: string
  scope: string
  status: string
  count: number
  items: MemoryItem[]
  error?: string
}

export interface SessionSnapshot {
  project: { id: string; name: string; root_path: string }
  session: Session
  messages: Message[]
  memory: MemoryPayload
  /** 当前模型的能力上限（重连即可见，不必等下一轮对话）。 */
  context?: ContextPayload
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
  /** 事件发生时间：前端按它把 messages 与 replay 归并成一条时间线（07 §4.6a）。 */
  occurred_at?: string
  data: Record<string, unknown>
}

/** message.segment 事件载荷：一个段落落库完成（与 message.created 同构 + source）。 */
export interface MessageSegmentData {
  message: Message
  /** 事件来源："" = 主 ReAct 轮，task:<id> = /plan 子任务，agent:<id> = /team worker。 */
  source?: string
  request_id?: string
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
  /** 规则加权总分（方案 08）。 */
  score?: number
  /**
   * 判定依据链（方案 08 §4.1）：`hard_rule:*` / `score:*` / `ml:*` / `calibration:*` /
   * `rule:*` / `learned:*` / `anti_downgrade:*` / `hysteresis:*`。
   * 文案渲染统一走 `utils/routerNotes.ts`（后端只给结构化枚举）。
   */
  notes?: string[]
  /** 本轮是否**真的切换了模型**（目标与当前一致时为 false）。 */
  switched?: boolean
  /** 路由耗时（毫秒）；load_ms 是首次加载 ML/语义模型的耗时。 */
  elapsed_ms?: number
  load_ms?: number
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

/** Skill 详情（GET /api/skills/{name}）：正文用于预览与编辑回填。 */
export interface SkillDetail extends SkillView {
  body: string
  layer: string
  path: string
  editable: boolean
}

export interface SkillWritePayload {
  body: string
  description?: string
  layer?: 'user' | 'project'
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

// ---- 项目工作区文件 ----

/** 目录项（`GET /api/projects/{id}/files` 的一层）。 */
export interface FileEntry {
  name: string
  /** 项目内 POSIX 相对路径（根为空串）。 */
  path: string
  type: 'dir' | 'file' | 'other'
  size: number
  mtime: string
  /** 软链接或 Windows junction。 */
  symlink: boolean
  broken: boolean
  /** 链接指向项目根之外（点开会被服务端拒绝）。 */
  outside: boolean
  /** 位于 IGNORED_DIRS 内（仅在 include_ignored 时出现）。 */
  ignored: boolean
}

export interface FileListing {
  path: string
  parent: string
  entries: FileEntry[]
  truncated: boolean
  limit: number
}

/**
 * 文件内容。三类护栏：`binary` 不下发内容；`lossy`（含无法解码字节）与
 * `truncated`（超 1MB）虽然能看，但**不能保存**（`editable` 为 false）。
 */
export interface FileContent {
  path: string
  size: number
  mtime: string
  /** 内容 sha256 前 16 位，保存时回传做冲突检测；截断文件为空。 */
  version: string
  binary: boolean
  lossy: boolean
  truncated: boolean
  line_ending: string
  content: string
  editable: boolean
  reason: string
}

export interface FileWriteResult {
  path: string
  size: number
  created: boolean
  forced: boolean
  line_ending: string
  version: string
}

export interface ToolStartedData {
  tool_call_id: string
  name: string
  arguments: string
  /** 事件来源（子任务标注用，07 §4.10）；主会话为空。 */
  source?: string
  request_id?: string
}

export interface ToolCompletedData {
  tool_call_id: string
  name: string
  ok: boolean
  output: string
  error: string
  duration_ms: number
  source?: string
  request_id?: string
}

export interface MessageDeltaData {
  kind: 'content' | 'thinking' | string
  text: string
  source?: string
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
  /** 该任务声明的资源范围（方案 15 §4.5）：续跑时用来给出"允许修改"的候选。 */
  resource_claims?: { pattern: string; access: string }[]
  /** 任务级失败分类（卡上逐任务解释原因）。 */
  failure_category?: string
  /** 「无法安全确定写入范围」而失败：可以在卡上补一个范围救回来。 */
  needs_scope?: boolean
  /** 写入范围候选（服务端保守提取，勾选才产生授权）。 */
  scope_candidates?: string[]
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
  /**
   * 这轮还能不能续跑（方案 15 §4.4）：收尾事件（team_failed / cancelled）携带。
   * 前端据此在团队卡上给「继续」按钮——不在前端自己推断，避免与服务端配额判断漂移。
   */
  resumable?: boolean
  /** 这个 Team 任务已经续跑过几次（超上限时服务端会拒）。 */
  resume_count?: number
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

/** 可选中的一项（对应 routivus/ask/models.py 的 AskOption）。 */
export interface AskOption {
  label: string
  /** 回灌给模型的取值；服务端构造时已兜底为非空（缺省等于 label）。 */
  value: string
}

/**
 * 一个待收集的问题字段（对应 routivus/ask/models.py 的 AskField）。
 *
 * 契约曾在 Web 端错位（前端按 name/label/options: string[] 渲染）：选项对象被
 * 当成 React 子节点直接把整棵组件树打崩，字段标签退化成 field_0。改字段名时
 * 两端必须同步。
 */
export interface AskField {
  /** 答案的键：提交时按它回填，模型按它取值。 */
  key: string
  question: string
  options: AskOption[]
  /** 允许在下拉之外手填自定义值。 */
  allow_custom: boolean
  /** 默认值：恰好等于某选项取值时预选，否则作为自定义输入的初稿。 */
  default: string
  required: boolean
}

/** 一次 ask_user（对应 routivus/ask/models.py 的 AskRequest）。 */
export interface AskRequest {
  id?: string
  /** 问题原文：问答卡的标题。 */
  prompt?: string
  fields?: AskField[]
  origin?: string
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
