import { useEffect, useState } from 'react'
import type {
  ContextPayload,
  MemoryPayload,
  PlanPayload,
  RouterState,
  Session,
} from '../../api/types'
import type { AuditTotals, UsageTotals } from '../../state/sessionTimeline'
import type { ConnState } from '../../ws/sessionSocket'
import { formatDateTime, formatNumber } from '../../utils/format'
import { reasonSummary } from '../../utils/routerNotes'

const TABS = ['Session', 'Plan', 'Memory', 'Safety'] as const
type TabName = (typeof TABS)[number]

const STATUS_TEXT: Record<string, string> = {
  idle: '空闲',
  running: '运行中',
  waiting_approval: '等待审批',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

/** 窗口来自哪一层（与后端 resolve_window_detail 的 source 对应）。 */
const WINDOW_SOURCE_TEXT: Record<string, string> = {
  env: '环境变量覆盖',
  model: '模型覆盖',
  provider: 'provider 默认',
  default: '默认值',
}

interface SidePanelProps {
  session: Session | null
  usage: UsageTotals
  audit: AuditTotals
  connection: ConnState
  plan: PlanPayload | null
  projectPath: string | null
  projectNotes: number
  memory: MemoryPayload | null
  onRefreshMemory: () => void
  memoryNotice: { kind: string; message: string } | null
  hitl: string | null
  router: RouterState | null
  contextWindow: number
  /** 会话当前模型的能力上限；换模型 / 路由换档后会变（见 plans/enhancement/06）。 */
  context: ContextPayload | null
  /** 文件抽屉接管右侧区域时收起。只隐藏、不卸载：来回开合不会丢掉当前页签。 */
  hidden?: boolean
}

export function SidePanel({
  session,
  usage,
  audit,
  connection,
  plan,
  projectPath,
  projectNotes,
  memory,
  onRefreshMemory,
  memoryNotice,
  hitl,
  router,
  contextWindow,
  context,
  hidden = false,
}: SidePanelProps) {
  const [tab, setTab] = useState<TabName>('Session')
  const usedTokens = session?.total_tokens ?? 0
  // 窗口以会话实时值为准（换模型、SmartRouter 换档都会变）；prop 只是还没连上
  // 会话时的基线，不再是构建期写死的常量。
  const effectiveWindow = context?.window ?? contextWindow
  const outputLimit = Math.max(0, context?.max_output ?? 0)
  const ratio = effectiveWindow > 0 ? Math.min(1, usedTokens / effectiveWindow) : 0
  const memoryItems = memory?.items ?? []
  const memoryEmptyHint =
    memory?.status === 'unavailable'
      ? `长期记忆库不可用${memory.error ? `：${memory.error}` : ''}`
      : memory === null
        ? '正在读取项目长期记忆…'
        : '这个项目还没有长期记忆。用 /save <内容> 保存一条，/memory list 查看全部。'

  // 打开 Memory 页签时才拉条目：快照自带的那份在切会话后可能已经过期。
  useEffect(() => {
    if (tab === 'Memory') onRefreshMemory()
  }, [tab, onRefreshMemory])

  return (
    <aside className={`side${hidden ? ' side-hide' : ''}`}>
      <div className="tabs">
        {TABS.map((name) => (
          <button
            type="button"
            key={name}
            className={`tab${tab === name ? ' active' : ''}`}
            onClick={() => setTab(name)}
          >
            {name}
          </button>
        ))}
      </div>
      <div className="side-scroll">
        {tab === 'Session' ? (
          <>
            <div className="sec-title">SESSION</div>
            {/* 会话默认：手动配置的 provider/model。智能路由开启时它们**不等于**
                本轮实际在跑的模型，所以另起一组「本轮实际」，避免同名 key 造成歧义
                （方案 08 §0/§4.3）。 */}
            <div className="kv">
              <span>Provider</span>
              <span className="mono">{session?.active_provider || '—'}</span>
            </div>
            <div className="kv">
              <span>Model</span>
              <span className="mono">{session?.active_model || '—'}</span>
            </div>
            {router?.enabled ? (
              <>
                <div className="sec-title" style={{ marginTop: 10 }}>
                  本轮实际
                </div>
                <div className="router-block">
                  <div className="router-block-head">
                    <span className={`router-badge${router.error ? ' warn' : ''}`}>
                      {router.error ? '路由失败' : router.tier || '待路由'}
                    </span>
                    {router.configured === false ? <span className="router-tag">回落 active</span> : null}
                    {typeof router.confidence === 'number' && router.tier ? (
                      <span className="router-tag">置信 {(router.confidence * 100).toFixed(0)}%</span>
                    ) : null}
                    {typeof router.elapsed_ms === 'number' ? (
                      <span className="router-tag">{router.elapsed_ms} ms</span>
                    ) : null}
                  </div>
                  <div className="kv">
                    <span>Provider</span>
                    <span className="mono">{router.provider || '—'}</span>
                  </div>
                  <div className="kv">
                    <span>Model</span>
                    <span className="mono">{router.model || '—'}</span>
                  </div>
                  {router.tier ? (
                    <div className="kv">
                      <span>依据</span>
                      <span className="router-reason">
                        {reasonSummary(router.notes)}
                        {(router.notes?.length ?? 0) > 1 ? (
                          <span className="dim"> · 另 {router.notes!.length - 1} 条见顶栏 ⚡</span>
                        ) : null}
                      </span>
                    </div>
                  ) : null}
                  {router.switched === false && router.model ? (
                    <div className="hint" style={{ marginTop: 2 }}>
                      目标与当前模型一致，本轮未切换。
                    </div>
                  ) : null}
                  {router.error ? (
                    <div className="hint" style={{ marginTop: 2 }}>
                      {router.error}
                    </div>
                  ) : null}
                </div>
              </>
            ) : null}
            <div className="kv">
              <span>Status</span>
              <span className={session?.status === 'failed' ? 'status-error' : 'status-idle'}>
                {STATUS_TEXT[session?.status ?? 'idle'] ?? session?.status ?? '—'}
              </span>
            </div>
            <div className="kv">
              <span>连接</span>
              <span className="mono">{connection}</span>
            </div>

            <div className="sec-title">CONTEXT</div>
            <div className="kv">
              <span>窗口</span>
              <span className="num">{formatNumber(effectiveWindow)} tk</span>
            </div>
            {context ? (
              <div className="kv">
                <span>当前模型</span>
                <span className="mono">
                  {context.model || '—'}
                  {context.source ? ` · ${WINDOW_SOURCE_TEXT[context.source] ?? context.source}` : ''}
                </span>
              </div>
            ) : null}
            <div className="kv">
              <span>使用率</span>
              <span className="num">{(ratio * 100).toFixed(1)}%</span>
            </div>
            <div className="bar">
              <i style={{ width: `${Math.max(ratio * 100, ratio > 0 ? 2 : 0)}%` }} />
            </div>
            <div className="kv">
              <span>已用</span>
              <span className="num">{formatNumber(session?.total_tokens ?? 0)} tk</span>
            </div>
            <div className="kv">
              <span>最大输出</span>
              <span className="num">
                {outputLimit > 0
                  ? `${formatNumber(outputLimit)} tk · 发送 ${context?.output_field || '（未发送）'}`
                  : '不限制（不发送）'}
              </span>
            </div>
            <div className="kv">
              <span>最大输入</span>
              <span className="num">{formatNumber(Math.max(0, effectiveWindow - outputLimit))} tk</span>
            </div>
            <div className="hint">
              最大输入是推导值（窗口 − 最大输出），不可单独配置。窗口与输出上限按「当前模型」
              计算，换模型或智能路由换档后都会变化。
            </div>
            <div className="kv">
              <span>已用来源</span>
              <span>会话快照 + usage 事件累计</span>
            </div>

            <div className="sec-title">使用量</div>
            <div className="kv">
              <span>Prompt</span>
              <span className="num">{formatNumber(usage.prompt)}</span>
            </div>
            <div className="kv">
              <span>Completion</span>
              <span className="num">{formatNumber(usage.completion)}</span>
            </div>
            <div className="kv">
              <span>Total</span>
              <span className="num">{formatNumber(usage.total)}</span>
            </div>

            <div className="sec-title">COMPACTION</div>
            <div className="kv">
              <span>最近事件</span>
              <span>{memoryNotice ? memoryNotice.kind : '—'}</span>
            </div>
          </>
        ) : null}

        {tab === 'Plan' ? (
          <>
            <div className="sec-title">当前模式</div>
            <div className="kv">
              <span>模式</span>
              <span>{plan ? '计划' : '直接执行（ReAct）'}</span>
            </div>
            <div className="kv">
              <span>主任务</span>
              <span>{plan?.plan?.goal ?? '—'}</span>
            </div>
            <div className="sec-title">步骤</div>
            <ul className="steps">
              {(plan?.plan?.tasks ?? []).map((task) => {
                const cls =
                  task.status === 'done'
                    ? 'mk-done'
                    : task.status === 'running'
                      ? 'mk-run'
                      : task.status === 'failed'
                        ? 'mk-fail'
                        : 'mk-todo'
                const glyph =
                  task.status === 'done'
                    ? '✓'
                    : task.status === 'running'
                      ? '●'
                      : task.status === 'failed'
                        ? '✕'
                        : '○'
                return (
                  <li key={task.id}>
                    <span className={`mk ${cls}`}>{glyph}</span>
                    <span
                      className={
                        task.status === 'done' ? 'step-done' : task.status === 'running' ? 'step-run' : ''
                      }
                    >
                      {task.title}
                    </span>
                  </li>
                )
              })}
            </ul>
            {!plan ? <div className="hint">当前会话未处于计划模式。</div> : null}
            <div className="hint">
              使用 <code>/plan go</code> 继续、<code>/plan edit</code> 追加要求、<code>/plan abort</code> 终止。
            </div>
          </>
        ) : null}

        {tab === 'Memory' ? (
          <>
            <div className="sec-title">项目路径</div>
            <div className="kv">
              <span>root</span>
              <span className="mono">{projectPath ?? '—'}</span>
            </div>
            <div className="kv">
              <span>项目笔记</span>
              <span className="num">{projectNotes}</span>
            </div>
            <div className="sec-title">记忆条目</div>
            <div className="kv">
              <span>条数</span>
              <span className="num">{memory?.count ?? 0}</span>
            </div>
            {memoryItems.length > 0 ? (
              memoryItems.map((entry) => (
                <div className="mem-item" key={entry.id}>
                  <div className="mem-meta">
                    #{entry.id}
                    {entry.updated_at ? ` · ${formatDateTime(entry.updated_at)}` : ''}
                    {entry.source ? ` · ${entry.source}` : ''}
                  </div>
                  <div className="mem-body">{entry.content}</div>
                </div>
              ))
            ) : (
              <div className="hint">{memoryEmptyHint}</div>
            )}
            {memoryItems.length > 0 && (memory?.count ?? 0) > memoryItems.length ? (
              <div className="hint">
                仅显示最近 {memoryItems.length} 条（共 {memory?.count} 条）。
              </div>
            ) : null}
            {memoryNotice ? (
              <div className="mem-item">
                <div className="mem-meta">{memoryNotice.kind}</div>
                <div className="mem-body">{memoryNotice.message}</div>
              </div>
            ) : null}
            <div className="hint">
              项目长期记忆由 <code>/save</code>、<code>/memory list</code>、<code>/memory delete</code> 管理；
              笔记与 Memory 是两套独立数据。
            </div>
          </>
        ) : null}

        {tab === 'Safety' ? (
          <>
            <div className="sec-title">HITL 状态</div>
            <div className="kv">
              <span>审批</span>
              <span>
                {hitl ? <span className="pill-ok">已启用（服务端托管）</span> : <span className="pill-warn">未知</span>}
              </span>
            </div>
            <div className="sec-title">审批队列</div>
            <div className="kv">
              <span>累计审批事件</span>
              <span className="num">{audit.approvals}</span>
            </div>
            <div className="sec-title">会话审计</div>
            <div className="kv">
              <span>工具调用</span>
              <span className="num">{audit.tool_calls}</span>
            </div>
            <div className="kv">
              <span>工具失败</span>
              <span className="num">{audit.tool_failures}</span>
            </div>
            <div className="hint">
              策略层黑名单命令与越界路径始终拒绝，不可被审批放行绕过。完整审计写入项目
              <code> .routivus/audit.log</code>。
            </div>
          </>
        ) : null}
      </div>
    </aside>
  )
}
