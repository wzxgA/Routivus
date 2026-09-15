import { useState } from 'react'
import type { UsageBucket, UsageDay, UsageSummary, UsageTier } from '../../api/types'
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
 * - KPI 给 total，拆分（prompt ↑ / completion ↓）放在小字与悬停里
 * - 折线位置用**堆叠柱**：每天一根，prompt 在下、completion 在上。选柱而不是折线，
 *   是因为用量是"按天离散"的量，柱既看得出趋势也点得准（悬停到某天不会像折线那样
 *   需要在两条线之间做插值判断）
 * - 按档位分布单独一块：本版本之前的事件没有归因字段，会以「未标注」出现，这是数据
 *   事实，不隐藏也不猜
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
        <>
          <UsageChart daily={summary.daily} />
          {summary.by_tier.length > 0 ? <TierBars tiers={summary.by_tier} days={summary.days} /> : null}
        </>
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

/**
 * 30 天堆叠柱。
 *
 * 用 `preserveAspectRatio="none"` 让 SVG 横向铺满：柱子不需要保持宽高比，而轴文字放在
 * SVG 外面就不会被拉伸（这也省掉了按容器宽度重算 viewBox 的逻辑）。
 */
function UsageChart({ daily }: { daily: UsageDay[] }) {
  const [hover, setHover] = useState<number | null>(null)
  const width = 720
  const height = 92
  const slot = width / Math.max(1, daily.length)
  const barWidth = Math.max(1, slot - 2)
  const peak = Math.max(1, ...daily.map((day) => day.total))
  const scaled = (value: number) => Math.round((value / peak) * (height - 6))
  const activeIndex = hover ?? -1
  const active = activeIndex >= 0 ? daily[activeIndex] : null

  return (
    <div className="usage-chart">
      <svg
        className="usage-svg"
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`最近 ${daily.length} 天 token 用量`}
      >
        {daily.map((day, index) => {
          const x = index * slot + (slot - barWidth) / 2
          const promptHeight = scaled(day.prompt)
          const completionHeight = scaled(day.completion)
          return (
            <g key={day.date}>
              {/* 命中区铺满整格：柱子很细时也能悬停到（含空日子） */}
              <rect
                className="usage-hit"
                x={index * slot}
                y={0}
                width={slot}
                height={height}
                onMouseEnter={() => setHover(index)}
                onMouseLeave={() => setHover(null)}
              />
              {promptHeight > 0 ? (
                <rect
                  className="usage-bar-prompt"
                  x={x}
                  y={height - promptHeight}
                  width={barWidth}
                  height={promptHeight}
                />
              ) : null}
              {completionHeight > 0 ? (
                <rect
                  className="usage-bar-completion"
                  x={x}
                  y={height - promptHeight - completionHeight}
                  width={barWidth}
                  height={completionHeight}
                />
              ) : null}
            </g>
          )
        })}
      </svg>
      <div className="usage-axis">
        <span>{daily[0]?.date.slice(5)}</span>
        <span>{daily[Math.floor(daily.length / 2)]?.date.slice(5)}</span>
        <span>{daily[daily.length - 1]?.date.slice(5)}</span>
        <span className="usage-axis-peak">峰值 {formatTokens(peak)}</span>
      </div>
      {active ? (
        <div
          className="usage-tip"
          style={{ left: `${((activeIndex + 0.5) / daily.length) * 100}%` }}
        >
          <div className="usage-tip-date">{active.date}</div>
          <div>
            合计 <b>{formatTokens(active.total)}</b>
            {active.turns > 0 ? ` · ${active.turns} 轮` : ''}
          </div>
          <div className="dim">
            ↑ {formatTokens(active.prompt)} · ↓ {formatTokens(active.completion)}
          </div>
        </div>
      ) : null}
    </div>
  )
}

function TierBars({ tiers, days }: { tiers: UsageTier[]; days: number }) {
  const peak = Math.max(1, ...tiers.map((item) => item.total))
  const onlyUnlabeled = tiers.every((item) => item.tier === '未标注')
  return (
    <div className="usage-tiers">
      <div className="usage-tiers-title">按档位（近 {days} 天）</div>
      {tiers.map((item) => (
        <div className="usage-tier" key={item.tier}>
          <span className="usage-tier-name">{item.tier}</span>
          <span className="usage-tier-bar">
            <i style={{ width: `${Math.max(2, (item.total / peak) * 100)}%` }} />
          </span>
          <span className="usage-tier-value">{formatTokens(item.total)}</span>
        </div>
      ))}
      {onlyUnlabeled ? (
        <div className="hint">
          档位归因从当前版本开始记录：此前的用量事件没有档位字段，全部归入「未标注」。
        </div>
      ) : null}
    </div>
  )
}
