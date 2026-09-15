import type { RouterEvolutionView, RouterLimits } from '../../api/types'
import { formatDateTime } from '../../utils/format'

function stamp(seconds: number | undefined): string {
  if (!seconds) return '—'
  return formatDateTime(new Date(seconds * 1000).toISOString())
}

function ratio(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(3)
}

const TRIGGER_TEXT: Record<string, string> = {
  startup: '启动检查',
  rounds: '轮次检查',
  manual: '手动触发',
  cli: '命令行',
}

const DECISION_TEXT: Record<string, string> = {
  skipped: '跳过',
  replaced: '已替换产物',
  rejected: '被不劣门拒绝',
  error: '出错',
}

/** 跳过原因码 → 人话（与 evolve.gate() / 不劣门的原因码一一对应）。 */
const REASON_TEXT: Record<string, string> = {
  not_enough_tiers: '至少两个档位有标签',
  not_enough_per_tier: '单档样本不足',
  not_enough_total: '总量不足',
  cooldown: '冷却期内',
  not_enough_new: '自上次成功新增不足',
  worse_than_current: '候选准确率低于当前产物',
  unevaluable: '无法评估',
  backup_failed: '备份失败，放弃替换',
  locked: '已有演化进程在跑',
  disabled: '自动演化已关闭',
}

/**
 * 进化：门槛进度 + 决策历史。
 *
 * 门槛逐条列出**当前值 / 门槛**，因为「为什么还没进化」只有一个诚实答案：差在哪一条。
 * 只显示"暂无数据"是这页最容易犯的错，所以空态也走同一套渲染。
 */
export function EvolutionPanel({
  evolution,
  limits,
}: {
  evolution: RouterEvolutionView
  limits: RouterLimits
}) {
  const { state } = evolution
  const history = [...evolution.history].reverse()

  return (
    <div className="big-card">
      <div className="card-head">
        <div className="card-name">本地进化</div>
        <span className="ri-stamp">
          自动演化 {evolution.auto_evolve ? '开' : '关'} · 已成功 {state.evolve_count} 次
        </span>
      </div>
      <div className="card-desc">
        五道门槛全部满足才会训练；训练出的候选产物还要过<b>不劣门</b>（在同一 holdout 上的加权
        准确率不得低于当前在用产物），否则丢弃并记下原因。holdout 按时间切最近{' '}
        {Math.round(limits.holdout_ratio * 100)}%。
      </div>

      <div className="ri-gates">
        {evolution.gates.map((gate) => (
          <div className={`ri-gate${gate.satisfied ? ' ok' : ''}`} key={gate.key}>
            <span className="ri-gate-mark">{gate.satisfied ? '✓' : '·'}</span>
            <span className="ri-gate-label">{gate.label}</span>
            <span className="ri-gate-detail">{gate.detail}</span>
          </div>
        ))}
      </div>

      <div className="ri-metrics">
        <div className="ri-metric">
          <span className="ri-metric-label">上次尝试</span>
          <span className="ri-metric-value mono">
            {state.last_attempt_at ? stamp(state.last_attempt_at).slice(5) : '从未'}
          </span>
          <span className="ri-metric-sub">
            {state.last_decision
              ? `${DECISION_TEXT[state.last_decision] ?? state.last_decision}${
                  state.last_reason ? `：${REASON_TEXT[state.last_reason] ?? state.last_reason}` : ''
                }`
              : '还没有尝试过'}
          </span>
        </div>
        <div className="ri-metric">
          <span className="ri-metric-label">上次成功</span>
          <span className="ri-metric-value mono">
            {state.last_success_at ? stamp(state.last_success_at).slice(5) : '从未'}
          </span>
          <span className="ri-metric-sub">自那以后新增 {evolution.new_since_success} 条样本</span>
        </div>
        <div className="ri-metric">
          <span className="ri-metric-label">上次 holdout / 基线</span>
          <span className="ri-metric-value mono">
            {ratio(state.last_holdout_acc)} / {ratio(state.last_baseline_acc)}
          </span>
          <span className="ri-metric-sub">两者相等或更高才会替换产物</span>
        </div>
        <div className="ri-metric">
          <span className="ri-metric-label">冷却</span>
          <span className="ri-metric-value mono">
            {evolution.cooldown_remaining_days > 0
              ? `${evolution.cooldown_remaining_days.toFixed(1)} 天`
              : '可尝试'}
          </span>
          <span className="ri-metric-sub">门槛 {limits.evolve_cooldown_days} 天</span>
        </div>
      </div>

      <div className="ri-sub-title" style={{ marginTop: 12 }}>
        决策历史（最近 {history.length} 次）
      </div>
      {history.length === 0 ? (
        <div className="hint">
          还没有任何决策记录：门槛没过时不会写状态，只有真的进到训练（或被不劣门拒绝）才会落一行。
        </div>
      ) : (
        <div className="ri-table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>时间</th>
                <th>触发</th>
                <th>结果</th>
                <th>原因</th>
                <th>样本</th>
                <th>holdout / 基线</th>
              </tr>
            </thead>
            <tbody>
              {history.map((row, index) => (
                <tr key={`${row.ts}-${index}`}>
                  <td className="mono">{stamp(row.ts).slice(5)}</td>
                  <td>{TRIGGER_TEXT[row.trigger ?? ''] ?? row.trigger ?? '—'}</td>
                  <td>
                    {row.decision === 'replaced' ? (
                      <span className="ri-up">{DECISION_TEXT.replaced}</span>
                    ) : row.decision === 'rejected' ? (
                      <span className="ri-warn">{DECISION_TEXT.rejected}</span>
                    ) : (
                      DECISION_TEXT[row.decision ?? ''] ?? row.decision ?? '—'
                    )}
                  </td>
                  <td>{row.reason ? REASON_TEXT[row.reason] ?? row.reason : '—'}</td>
                  <td className="mono">
                    {row.samples ?? '—'}
                    {row.n_train !== undefined ? (
                      <span className="dim">
                        （训练 {row.n_train} / 验证 {row.n_holdout}）
                      </span>
                    ) : null}
                  </td>
                  <td className="mono">
                    {ratio(row.holdout_acc)} / {ratio(row.baseline_acc)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
