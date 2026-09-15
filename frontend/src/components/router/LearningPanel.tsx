import type {
  RouterCalibrationView,
  RouterLimits,
  RouterRuleView,
  RouterSamplesView,
} from '../../api/types'
import { TIER_NAMES } from '../../utils/routerNotes'

const SIGNAL_TEXT: Record<string, string> = {
  clarify: '追问 / 否定',
  cmd_retry: '重试类输入',
  interrupt: '中途中断',
  short_high_tier: '短问句落高档',
}

const BUILD_STAT_TEXT: Record<string, string> = {
  labeled: '人工标注',
  feedback: '隐式反馈',
  dropped_hard_rule: '被硬规则剔除',
  dropped_no_label: '推不出标签',
  dropped_conflict: '自相矛盾丢弃',
  with_sem: '带语义向量',
}

function percent(value: number, max: number): number {
  if (!max) return 0
  return Math.min(100, (Math.abs(value) / max) * 50)
}

function formatBytes(bytes: number): string {
  if (!bytes) return '—'
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function RuleRow({ rule }: { rule: RouterRuleView }) {
  return (
    <tr>
      <td className="mono">
        {rule.feature} {rule.op} {rule.value}
      </td>
      <td>
        {rule.action > 0 ? (
          <span className="ri-up">升一档</span>
        ) : (
          <span className="ri-down">降一档</span>
        )}
      </td>
      <td className="mono">{rule.confidence.toFixed(2)}</td>
      <td className="mono">{rule.support}</td>
    </tr>
  )
}

/**
 * 学习数据：L1 校准偏置、自学习规则、以及样本积累情况。
 *
 * 这一块回答的是"为什么路由器还没有变得更懂我"：偏置要单档样本够了才生效、规则要
 * 支持度与多数派都够才生成——所以**门槛与当前值必须并排显示**，只给数字读者无从判断。
 */
export function LearningPanel({
  calibration,
  rules,
  samples,
  limits,
}: {
  calibration: RouterCalibrationView
  rules: { degraded: boolean; items: RouterRuleView[] }
  samples: RouterSamplesView
  limits: RouterLimits
}) {
  const biasMax = limits.max_bias || 0.15
  const minSamples = limits.calibration_min_samples || 20
  const signalRows = Object.keys(limits.signals)

  return (
    <>
      <div className="ri-grid">
        <div className="ri-card">
          <div className="ri-card-name">档位偏置（校准）</div>
          <div className="card-desc">
            单个档位的加权样本达到 {minSamples} 条才开始校准，偏置夹在 ±{biasMax} 内；
            阈值调整 {calibration.threshold_adjust.toFixed(3)}（上限 ±{limits.max_threshold_adjust}）。
            {calibration.degraded ? ' 校准文件损坏，已按空态处理。' : ''}
          </div>
          {TIER_NAMES.map((tier) => {
            const bias = calibration.bias[tier] ?? 0
            const count = calibration.samples[tier] ?? 0
            return (
              <div className="ri-bias" key={tier} data-tier={tier}>
                <span className="ri-bias-name">{tier}</span>
                <span className="ri-bias-track">
                  <span className="ri-bias-zero" />
                  <span
                    className={`ri-bias-fill${bias < 0 ? ' neg' : ''}`}
                    style={
                      bias < 0
                        ? { right: '50%', width: `${percent(bias, biasMax)}%` }
                        : { left: '50%', width: `${percent(bias, biasMax)}%` }
                    }
                  />
                </span>
                <span className="ri-bias-value mono">{bias.toFixed(3)}</span>
                <span className="ri-bias-count">
                  {count.toFixed(0)}/{minSamples}
                </span>
              </div>
            )
          })}
          <div className="hint" style={{ marginTop: 6 }}>
            偏置为负 = 该档信号偏弱（边界输入会升一档）；为正 = 偏强（会降一档）。
          </div>
        </div>

        <div className="ri-card">
          <div className="ri-card-name">自学习规则（{rules.items.length} 条）</div>
          <div className="card-desc">
            同一特征谓词的加权支持度 ≥{limits.rule_min_support} 且多数派占比 ≥
            {limits.rule_min_precision} 才生成，最多 {limits.rules_max} 条，置信度上限{' '}
            {limits.rule_max_confidence}；命中只偏移 ±1 档。
            {rules.degraded ? ' 规则文件损坏，已按空态处理。' : ''}
          </div>
          {rules.items.length === 0 ? (
            <div className="hint">
              还没有规则：样本要么不够，要么在同一谓词上没有明显方向。规则在进程启动时重写
              ——攒够样本后重启后端才会出现在这里。
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>谓词</th>
                  <th>动作</th>
                  <th>置信度</th>
                  <th>支持度</th>
                </tr>
              </thead>
              <tbody>
                {rules.items.map((rule) => (
                  <RuleRow key={`${rule.feature}${rule.op}${rule.value}`} rule={rule} />
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="ri-grid">
        <div className="ri-card">
          <div className="ri-card-name">隐式信号</div>
          <div className="card-desc">
            只有这几类行为会产生学习信号（共 {samples.feedback_lines} 行记录、
            {samples.available_samples} 条可用样本、{samples.unique_texts} 个不同输入）。
            {samples.degraded ? ' 样本构建失败，已按空态处理。' : ''}
          </div>
          <table className="table">
            <thead>
              <tr>
                <th>行为</th>
                <th>方向</th>
                <th>权重</th>
                <th>次数</th>
              </tr>
            </thead>
            <tbody>
              {signalRows.map((name) => {
                const meta = limits.signals[name]
                const counts = samples.by_signal[name] ?? { up: 0, down: 0 }
                return (
                  <tr key={name}>
                    <td>{SIGNAL_TEXT[name] ?? name}</td>
                    <td>{meta.upgrade ? '升档' : '降档'}</td>
                    <td className="mono">{meta.weight}</td>
                    <td className="mono">
                      {counts.up + counts.down}
                      <span className="dim">
                        （升 {counts.up} / 降 {counts.down}）
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div className="hint" style={{ marginTop: 6 }}>
            本站的停止按钮不产生信号：「中途中断」只在旧 TUI 的内联循环里采集，所以这里长期为 0。
          </div>
        </div>

        <div className="ri-card">
          <div className="ri-card-name">样本库与去噪</div>
          <dl className="ri-kv">
            <div>
              <dt>近 7 / 30 天新增</dt>
              <dd>
                {samples.new_7d} / {samples.new_30d} 条
              </dd>
            </div>
            <div>
              <dt>加权样本总量</dt>
              <dd>{samples.weighted_total.toFixed(2)}</dd>
            </div>
            <div>
              <dt>本地语义样本</dt>
              <dd>
                {samples.sem_samples.count} / {samples.sem_samples.max} 条 ·{' '}
                {formatBytes(samples.sem_samples.bytes)}
              </dd>
            </div>
            <div>
              <dt>本地语义头（L3）</dt>
              <dd>
                {samples.semantic_head.present
                  ? `${samples.semantic_head.tiers} 个质心 · ${samples.semantic_head.dim} 维` +
                    (samples.semantic_head.train_acc !== null
                      ? ` · 训练准确率 ${samples.semantic_head.train_acc.toFixed(3)}`
                      : '')
                  : '还没有训练出来'}
              </dd>
            </div>
            <div>
              <dt>按档位（可用样本）</dt>
              <dd>
                {TIER_NAMES.map((tier) => `${tier} ${samples.by_tier[tier] ?? 0}`).join(' · ')}
              </dd>
            </div>
          </dl>
          <div className="ri-sub-title">样本构建过程</div>
          <div className="ri-chips">
            {Object.entries(samples.build_stats).map(([key, value]) => (
              <span className="ri-chip" key={key}>
                {BUILD_STAT_TEXT[key] ?? key} <b>{value}</b>
              </span>
            ))}
          </div>
          <div className="hint" style={{ marginTop: 6 }}>
            样本按输入的短哈希去重（同一句话只算一条），推不出档位或被硬规则命中的会被剔除，
            所以可用样本总是少于原始行数。样本库只存向量与哈希，<b>不存原文</b>。
          </div>
        </div>
      </div>
    </>
  )
}
