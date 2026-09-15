import type { UsageBucket, UsageSummary, UsageTier } from '../../api/types'
import { formatTokens } from '../../utils/format'

interface UsagePanelProps {
  summary: UsageSummary | null
  error: string | null
}

/** 两条口径差到这个比例才值得提示：小差异是正常的（事件与会话快照的写入时机差一轮）。 */
const DRIFT_NOTICE_PCT = 5

/**
 * 首页 token 面板（方案 12）。
 *
 * 口径全部来自后端 `/api/usage/summary`，这里只做展示：
 *
 * - KPI 给 total，拆分（prompt ↑ / completion ↓）放在小字里
 * - 按档位分布单独一块，四档各一色（色值随主题切换，见 app.css 的 --tier-*）
 * - 「未标注」不展示：那是本版本之前没有归因字段的历史存量，一条顶到天的灰条会把
 *   真正要看的四档压成看不见。后端照旧返回，只是这里不画（数据不猜、也不回填）
 * - 两条口径（逐轮事件 vs 会话累计）差得多时明说一句，而不是给个看起来精确的数字
 */
export function UsagePanel({ summary, error }: UsagePanelProps) {
  if (error) {
    return (
      <section className="usage-card">
        <div className="usage-head">
          <div className="card-name">Token 用量</div>
        </div>
        <div className="hint">用量统计暂不可用：{error}</div>
      </section>
    )
  }
  if (!summary) {
    return (
      <section className="usage-card">
        <div className="usage-head">
          <div className="card-name">Token 用量</div>
        </div>
        <div className="hint">正在统计…</div>
      </section>
    )
  }

  const lastWeek = summary.daily.slice(-7).reduce(
    (sum, day) => sum + day.total,
    0,
  )
  const blank = summary.lifetime.total === 0 && summary.range.total === 0

  return (
    <section className="usage-card">
      <div className="usage-head">
        <div className="card-name">Token 用量</div>
        <span className="spacer" />
        <span className="usage-note">按本地日统计（{summary.timezone}），含上下文压缩的用量</span>
      </div>

      <div className="usage-kpis">
        <Kpi label="今日" bucket={summary.today} />
        <Kpi label="近 7 天" bucket={{ ...summary.today, total: lastWeek }} onlyTotal />
        <Kpi label="近 30 天" bucket={summary.range} />
        <Kpi label="累计" bucket={summary.lifetime} extra={`${summary.lifetime.sessions} 个会话`} />
      </div>

      {blank ? (
        <div className="hint" style={{ marginTop: 10 }}>
          还没有可统计的用量：发一轮消息后这里会出现数据。
        </div>
      ) : (
        <TierBars tiers={summary.by_tier} days={summary.days} />
      )}

      {summary.drift.diff_pct >= DRIFT_NOTICE_PCT ? (
        <div className="usage-drift">
          逐轮事件合计 {formatTokens(summary.drift.events_total)}，会话累计{' '}
          {formatTokens(summary.drift.sessions_total)}（相差 {summary.drift.diff_pct}%）：
          早期事件可能已被裁剪，区间数字以事件为准。
        </div>
      ) : null}
    </section>
  )
}

function Kpi({
  label,
  bucket,
  onlyTotal = false,
  extra,
}: {
  label: string
  bucket: UsageBucket
  /** 只有 total 有意义时（近 7 天是从 daily 现算的，没有可靠的轮次/拆分） */
  onlyTotal?: boolean
  extra?: string
}) {
  return (
    <div className="usage-kpi">
      <div className="usage-kpi-label">{label}</div>
      <div className="usage-kpi-value">{formatTokens(bucket.total)}</div>
      {onlyTotal ? (
        <div className="usage-kpi-sub">
          <span className="dim">tk</span>
        </div>
      ) : (
        <div className="usage-kpi-sub">
          <span title="输入（prompt）">↑ {formatTokens(bucket.prompt)}</span>
          <span title="输出（completion）">↓ {formatTokens(bucket.completion)}</span>
          <span className="dim">{bucket.turns} 轮</span>
        </div>
      )}
      {extra ? <div className="usage-kpi-sub dim">{extra}</div> : null}
    </div>
  )
}

/** 后端给没有档位字段的历史事件用的归入标签（storage.usage_summary）。 */
const UNLABELED_TIER = '未标注'

/**
 * 按档位分布。四档各一色，颜色由 `data-tier` 在 CSS 里映射到 --tier-*，
 * 主题切换只换变量、不需要 React 重渲染。
 *
 * 比例尺按**展示出来的档位**取峰值：未标注一旦参与会把它顶到满格，
 * 剩下四档全被压成一条线，等于看不见。
 */
function TierBars({ tiers, days }: { tiers: UsageTier[]; days: number }) {
  const labeled = tiers.filter((item) => item.tier !== UNLABELED_TIER)
  const title = <div className="usage-tiers-title">按档位（近 {days} 天）</div>

  if (labeled.length === 0) {
    return (
      <div className="usage-tiers">
        {title}
        <div className="hint">
          档位归因从当前版本开始记录：此前的用量事件没有档位字段，暂时没有可展示的档位分布。
        </div>
      </div>
    )
  }

  const peak = Math.max(1, ...labeled.map((item) => item.total))
  return (
    <div className="usage-tiers">
      {title}
      {labeled.map((item) => (
        <div className="usage-tier" key={item.tier} data-tier={item.tier}>
          <span className="usage-tier-name">
            <i className="usage-tier-dot" aria-hidden="true" />
            {item.tier}
          </span>
          <span className="usage-tier-bar">
            <i style={{ width: `${Math.max(2, (item.total / peak) * 100)}%` }} />
          </span>
          <span className="usage-tier-value">{formatTokens(item.total)}</span>
        </div>
      ))}
    </div>
  )
}
