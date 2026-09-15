import type { RouterRuntimeView } from '../../api/types'
import { TIER_NAMES, describeNotes } from '../../utils/routerNotes'

const BUCKET_LABELS = ['<0.5', '0.5-0.7', '0.7-0.9', '>=0.9']

function ms(value: number | null): string {
  return value === null || value === undefined ? '—' : `${value} ms`
}

function stamp(value: string): string {
  return value ? value.replace('T', ' ').slice(5, 19) : '—'
}

/**
 * 运行态：每轮 `router.updated` 事件的统计。
 *
 * 四档分布刻意复用 token 面板那套 `.usage-tier` 与 `--tier-*` 色板——同一个档位在
 * 两个页面上必须是同一个颜色，否则读者要重新学一遍配色。
 *
 * 口径说明：失败轮没有档位与置信度，所以 `routed + 未知` = 总轮数，置信度分桶只
 * 统计路由成功的轮次。页面上把这句写出来，免得对不上账时被当成 bug。
 */
export function RuntimePanel({ runtime, days }: { runtime: RouterRuntimeView; days: number }) {
  const tiers = [...TIER_NAMES, 'unknown']
  const peak = Math.max(1, ...tiers.map((tier) => runtime.by_tier[tier] ?? 0))
  const bucketPeak = Math.max(1, ...BUCKET_LABELS.map((key) => runtime.confidence_buckets[key] ?? 0))

  if (runtime.total === 0) {
    return (
      <div className="big-card">
        <div className="card-head">
          <div className="card-name">每轮路由（近 {days} 天）</div>
        </div>
        <div className="hint">
          这段时间没有路由事件：智能路由可能没开，或者还没走过<b>普通对话轮</b>
          （/plan 与 /team 不路由，斜杠命令也不路由）。
        </div>
      </div>
    )
  }

  return (
    <div className="big-card">
      <div className="card-head">
        <div className="card-name">每轮路由（近 {days} 天）</div>
        <span className="ri-stamp">
          共 {runtime.total} 轮 · 路由成功 {runtime.routed} · 失败 {runtime.errors}
        </span>
      </div>
      <div className="card-desc">
        失败轮次没有档位与置信度，所以「档位合计 + 未知 = 总轮数」；置信度分桶只统计成功的轮次。
        {runtime.truncated ? '（事件数达到抓取上限，统计只覆盖最近一部分）' : ''}
      </div>

      <div className="ri-metrics">
        <div className="ri-metric">
          <span className="ri-metric-label">强规则命中占比</span>
          <span className="ri-metric-value">{(runtime.hard_rule_ratio * 100).toFixed(1)}%</span>
          <span className="ri-metric-sub">{runtime.hard_rule} / {runtime.routed} 轮</span>
        </div>
        <div className="ri-metric">
          <span className="ri-metric-label">真的换了模型</span>
          <span className="ri-metric-value">{runtime.switched}</span>
          <span className="ri-metric-sub">其余轮次目标档位与当前一致</span>
        </div>
        <div className="ri-metric">
          <span className="ri-metric-label">路由耗时 p50 / p95</span>
          <span className="ri-metric-value">{ms(runtime.latency_ms.route_p50)}</span>
          <span className="ri-metric-sub">
            p95 {ms(runtime.latency_ms.route_p95)} · 最大 {ms(runtime.latency_ms.route_max)}
          </span>
        </div>
        <div className="ri-metric">
          <span className="ri-metric-label">首次加载耗时 p95</span>
          <span className="ri-metric-value">{ms(runtime.latency_ms.load_p95)}</span>
          <span className="ri-metric-sub">只在冷启动那一轮计入</span>
        </div>
      </div>

      <div className="ri-split">
        <div>
          <div className="ri-sub-title">档位分布</div>
          {tiers.map((tier) => (
            <div className="usage-tier" key={tier} data-tier={tier}>
              <span className="usage-tier-name">
                <i className="usage-tier-dot" aria-hidden="true" />
                {tier === 'unknown' ? '未路由' : tier}
              </span>
              <span className="usage-tier-bar">
                <i style={{ width: `${Math.max(1, ((runtime.by_tier[tier] ?? 0) / peak) * 100)}%` }} />
              </span>
              <span className="usage-tier-value">{runtime.by_tier[tier] ?? 0}</span>
            </div>
          ))}
        </div>

        <div>
          <div className="ri-sub-title">置信度分布</div>
          {BUCKET_LABELS.map((label) => (
            <div className="usage-tier" key={label}>
              <span className="usage-tier-name">
                <i className="usage-tier-dot" aria-hidden="true" />
                {label}
              </span>
              <span className="usage-tier-bar">
                <i
                  style={{
                    width: `${Math.max(1, ((runtime.confidence_buckets[label] ?? 0) / bucketPeak) * 100)}%`,
                  }}
                />
              </span>
              <span className="usage-tier-value">{runtime.confidence_buckets[label] ?? 0}</span>
            </div>
          ))}
        </div>
      </div>

      {runtime.error_groups.length > 0 ? (
        <div className="ri-errors">
          <div className="ri-sub-title">失败原因（Top {runtime.error_groups.length}）</div>
          {runtime.error_groups.map((group) => (
            <div className="ri-error-row" key={group.text}>
              <span className="ri-error-count">{group.count}×</span>
              <span className="ri-error-text" title={group.text}>
                {group.text}
              </span>
            </div>
          ))}
        </div>
      ) : null}

      <div className="ri-sub-title" style={{ marginTop: 12 }}>
        最近 {runtime.recent.length} 轮
      </div>
      <div className="ri-table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>时间</th>
              <th>档位</th>
              <th>置信度</th>
              <th>判定依据</th>
              <th>耗时</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            {[...runtime.recent].reverse().map((turn, index) => {
              const notes = describeNotes(turn.notes)
              return (
                <tr key={`${turn.ts}-${index}`}>
                  <td className="mono">{stamp(turn.ts)}</td>
                  <td>
                    {turn.tier || <span className="ri-warn">未路由</span>}
                    {turn.hard_rule ? <span className="ri-tag">强规则</span> : null}
                  </td>
                  <td className="mono">
                    {turn.confidence === null ? '—' : turn.confidence.toFixed(2)}
                  </td>
                  <td className="ri-notes">
                    {notes.length === 0 ? (
                      <span className="dim">—</span>
                    ) : (
                      <>
                        {notes.slice(0, 2).map((note) => (
                          <span className="ri-note" key={note.raw}>
                            {note.label}
                          </span>
                        ))}
                        {notes.length > 2 ? (
                          <span className="ri-note dim">+{notes.length - 2}</span>
                        ) : null}
                      </>
                    )}
                  </td>
                  <td className="mono">{turn.elapsed_ms === null ? '—' : `${turn.elapsed_ms} ms`}</td>
                  <td>
                    {turn.error ? (
                      <span className="ri-warn" title={turn.error}>
                        失败
                      </span>
                    ) : turn.switched ? (
                      '已换模型'
                    ) : (
                      '沿用'
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
