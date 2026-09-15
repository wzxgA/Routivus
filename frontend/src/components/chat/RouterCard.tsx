import { useEffect, useRef, type CSSProperties } from 'react'
import type { RouterItem } from '../../state/sessionTimeline'
import type { RouterNoticeView } from '../../utils/routerNotes'
import { TierLevel } from '../common/TierLevel'

const TIER_LEVELS: Record<string, number> = {
  Basic: 1,
  Enhanced: 2,
  Superior: 3,
  Ultimate: 4,
}

const HEAD: Record<RouterNoticeView['kind'], { ico: string; label: string }> = {
  up: { ico: '⚡', label: '档位上调' },
  down: { ico: '⚡', label: '档位下调' },
  first: { ico: '⚡', label: '本轮档位' },
  fallback: { ico: '⚡', label: '档位回落' },
  frozen: { ico: '❄', label: '已冻结在' },
  held: { ico: '🛡', label: '沿用' },
  error: { ico: '⚠', label: '路由失败' },
}

/**
 * 对话流里的换档卡（方案 14 §4.5）：把原来的一行浅色提示升级为有重量的卡片
 * ——滑入 + 箭头划过 + 档位换色淡入 + 分数滚动。
 *
 * 触发判据与旧提示完全一致（只在 变化 / 回落 / 失败 / 冻结 / 防降级 时出现），
 * 不新增出现频率。两点语义取舍：
 * - **冻结 / 防降级不画箭头**：档位没变，箭头会自相矛盾，改为"停在档 + 锁定/盾"；
 * - **`fresh` 才播动画**：回放重建的条目（刷新 / 重连）走静态终态，不集体重播。
 */
export function RouterCard({ item }: { item: RouterItem }) {
  const { view, fresh } = item
  const head = HEAD[view.kind]
  const animate = Boolean(fresh)
  const changes = view.kind === 'up' || view.kind === 'down' || view.kind === 'fallback'
  const scoreRef = useRef<HTMLSpanElement | null>(null)

  // 规则总分 count-up：只在在线插入时跑一次；系统开了"减少动效"就直接落值
  // （CSS 媒体查询管不到 JS 定时器，必须在这里判）。
  useEffect(() => {
    const el = scoreRef.current
    if (!el) return
    const target = view.score
    if (typeof target !== 'number') return
    const reduced =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (!animate || reduced) {
      el.textContent = target.toFixed(1)
      return
    }
    let raf = 0
    const start = performance.now()
    const step = (now: number) => {
      const k = Math.min(1, (now - start) / 480)
      const eased = 1 - Math.pow(1 - k, 3)
      el.textContent = (target * eased).toFixed(1)
      if (k < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [animate, view.score])

  const tier = view.tier || view.prevTier
  const level = TIER_LEVELS[tier] ?? 0
  const scorePct = typeof view.score === 'number' ? Math.min(100, Math.round((view.score / 10) * 100)) : 0

  return (
    <div
      className={`tier-card tiered${animate ? ' play' : ''}${view.kind === 'frozen' ? ' frozen' : ''}`}
      data-tier={tier || undefined}
      style={{ '--pct': `${scorePct}%` } as CSSProperties}
    >
      <div className="tc-head">
        <span className="ico">{head.ico}</span>
        <span>
          {head.label}
          {view.kind === 'frozen' || view.kind === 'held' ? ` ${view.tier}` : ''}
        </span>
      </div>

      {view.kind === 'error' ? (
        <div className="tc-reason">
          本轮沿用当前模型
          {view.error ? <span className="dim">（{view.error}）</span> : null}
        </div>
      ) : (
        <>
          <div className="tc-flow">
            {changes && view.prevTier && view.prevTier !== view.tier ? (
              <>
                <div className="tc-tier tc-old" data-tier={view.prevTier}>
                  <span className="n">{view.prevTier}</span>
                  <TierLevel level={TIER_LEVELS[view.prevTier] ?? 0} />
                </div>
                <div className="tc-arrow">
                  <span className="rail" />
                  <span className="rail-fill" />
                  <span className="head">▶</span>
                </div>
                <div className="tc-tier tc-new" data-tier={view.tier}>
                  <span className="tc-swap">
                    {/* 旧档只参与"换色淡入"的动画；回放是冷渲染，多渲染一份会与新档
                        绝对定位重叠成两层字。 */}
                    {animate ? <span className="swap-old n">{view.prevTier}</span> : null}
                    <span className="swap-new n">{view.tier}</span>
                  </span>
                  <TierLevel level={level} />
                </div>
              </>
            ) : (
              // 冻结 / 防降级 / 首个结果：档位没有位移，不画箭头
              <div className="tc-tier tc-new" data-tier={view.tier}>
                <span className="n">{view.tier}</span>
                <TierLevel level={level} />
              </div>
            )}
          </div>

          <div className="tc-reason">
            {view.reason}
            {view.kind === 'fallback' && view.prevTier ? (
              <span className="dim">（由 {view.prevTier} 回落）</span>
            ) : null}
          </div>

          {typeof view.score === 'number' ? (
            <div className="tc-meta">
              <span>规则总分</span>
              <span className="snum" ref={scoreRef}>
                0.0
              </span>
              <span className="sbar">
                <i />
              </span>
            </div>
          ) : null}
        </>
      )}
    </div>
  )
}
