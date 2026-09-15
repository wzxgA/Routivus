import type { RouterState } from '../../api/types'
import { tierIndex } from '../../utils/routerNotes'
import { TierLevel } from '../common/TierLevel'

/**
 * 一次性动画编排（方案 14 §4.3），由 TopBar 按上一轮档位比较后下发：
 * 首个结果 `pop lightup`、换档 `glow lightup`、同档 `nudge`；播完由 TopBar 清空。
 */
export interface RouterMotion {
  classes: string
  /** 上一轮档位（'' = 本会话还没有结果）；浮层滑块的动画起点也用它。 */
  prevTier: string
}

interface RouterChipProps {
  router: RouterState
  /** 普通消息已发出、router.updated 尚未返回（本地推导，三条回落见方案 14 §4.3）。 */
  routing: boolean
  motion: RouterMotion
  onToggle: () => void
}

function chipTitle(router: RouterState): string {
  return (
    router.error ||
    `智能路由：普通对话轮按复杂度自动换档${
      router.tier ? `，本轮 ${router.tier}` : ''
    }${router.configured === false ? '（该档未显式配置，回落 active 模型）' : ''} — 点击查看依据`
  )
}

/**
 * 顶栏「⚡ 档位」chip 的结构化渲染（方案 14 批次 1）：
 * 四态机 = 待路由（呼吸）/ 路由中（shimmer）/ 档位（弹跳 + 逐格点亮）/ 失败（shake 后停灰）。
 *
 * 房屋规则（app.css 的对比度红线）：11px 的彩字在浅/深底上都不稳，文字保持中性色，
 * 档位色只上图形元素（边框 / ⚡ / 电量条，按 3:1 校核）。
 */
export function RouterChip({ router, routing, motion, onToggle }: RouterChipProps) {
  const tier = router.error ? '' : router.tier || ''

  // 路由中优先于一切：即使上一轮是失败态 / 已有旧档位，新一轮普通消息发出后
  // 都应显示 shimmer，直到 router.updated 到达或兜底超时把状态收回。
  if (routing) {
    return (
      <button type="button" className="chip router tier routing" onClick={onToggle} title={chipTitle(router)}>
        <span className="bolt">⚡</span>
        <span className="lbl">路由中…</span>
      </button>
    )
  }

  if (router.error) {
    return (
      <button type="button" className="chip router tier failed" onClick={onToggle} title={router.error}>
        <span className="bolt">⚠</span>
        <span className="lbl">路由失败</span>
        <TierLevel level={tierIndex(router.tier) + 1} />
      </button>
    )
  }

  if (!tier) {
    return (
      <button type="button" className="chip router tier pending" onClick={onToggle} title={chipTitle(router)}>
        <span className="bolt">⚡</span>
        <span className="lbl">待路由</span>
        <TierLevel level={0} />
      </button>
    )
  }

  const classes = `chip router tier tiered${motion.classes ? ' ' + motion.classes : ''}`
  return (
    <button
      type="button"
      className={classes}
      data-tier={tier}
      onClick={onToggle}
      title={chipTitle(router)}
    >
      <span className="bolt">⚡</span>
      <span className="lbl">{tier}</span>
      <TierLevel level={tierIndex(tier) + 1} lightup={motion.classes.includes('lightup')} />
    </button>
  )
}
