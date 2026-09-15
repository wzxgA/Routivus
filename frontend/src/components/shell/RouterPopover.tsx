import { useEffect, useRef } from 'react'
import type { RouterState, TierView } from '../../api/types'
import { TIER_NAMES, describeNotes, formatScore, tierIndex } from '../../utils/routerNotes'
import { TierLevel } from '../common/TierLevel'
import type { RouterMotion } from './RouterChip'

interface RouterPopoverProps {
  router: RouterState
  tiers: TierView[]
  motion: RouterMotion
}

/** 轨道行高（px）：滑块的 translateY 按它换算，改 CSS 里的 .trow 高度要同步这里。 */
const ROW_H = 30
/** 四档展示序：高档在上，与"档位上移"的心智一致（方案 14 §4.4）。 */
const DESC: readonly string[] = [...TIER_NAMES].reverse()

const RING_R = 26
const RING_C = 2 * Math.PI * RING_R

interface PipeNode {
  name: string
  val: string
  /** on = 参与并改了档；skip = 被拦（低置信 / 不可用），灰掉下沉；off = 本轮未涉及。 */
  state: 'on' | 'skip' | 'off'
}

/**
 * notes（有序因果链）→ 五节点流水线。未知枚举在流水线上没有节点，
 * 依据列表（下方 ul）会原样兜底显示，不会空白。
 */
function buildPipeline(notes: string[], score?: number): PipeNode[] {
  const ml = notes.find((note) => note.startsWith('ml:')) ?? ''
  let mlState: PipeNode['state'] = 'off'
  let mlVal = ''
  if (ml.startsWith('ml:idx=')) {
    mlState = 'on'
    const prob = /p=([\d.]+)/.exec(ml)?.[1]
    mlVal = prob ? `p=${prob}` : '采用'
  } else if (ml) {
    mlState = 'skip'
    mlVal = ml.startsWith('ml:skipped') ? '低置信' : '不可用'
  }

  const calibration = notes.find((note) => note.startsWith('calibration:')) ?? ''
  let calState: PipeNode['state'] = 'off'
  let calVal = ''
  if (calibration) {
    calState = 'on'
    const delta = Number(calibration.slice('calibration:'.length))
    calVal = delta > 0 ? `+${delta} 档` : `${delta} 档`
  }

  let postState: PipeNode['state'] = 'off'
  let postVal = ''
  if (notes.some((note) => note.startsWith('hysteresis:'))) {
    postState = 'on'
    postVal = '冻结'
  } else if (notes.some((note) => note.startsWith('anti_downgrade:'))) {
    postState = 'on'
    postVal = '防降级'
  } else if (notes.some((note) => note.startsWith('learned:'))) {
    postState = 'on'
    postVal = notes.find((note) => note.startsWith('learned:'))?.slice('learned:'.length).startsWith('-')
      ? '-1 档'
      : '+1 档'
  } else {
    const rule = notes.find((note) => note.startsWith('rule:'))
    if (rule) {
      postState = 'on'
      postVal = rule.slice('rule:'.length)
    }
  }

  return [
    { name: '特征提取', val: '', state: 'on' },
    {
      name: '规则打分',
      val: typeof score === 'number' ? formatScore(score) : '',
      state: notes.some((note) => note.startsWith('score:') || note.startsWith('hard_rule:')) ? 'on' : 'off',
    },
    { name: 'ML 精判', val: mlVal, state: mlState },
    { name: '校准偏置', val: calVal, state: calState },
    { name: '后处理', val: postVal, state: postState },
  ]
}

/**
 * 顶栏「⚡ 档位」chip 的浮层（方案 08 §4.3 起，方案 14 批次 2 重构）：
 *
 * 表格 → ① 四档竖排电平表（当前档滑块高亮，换档时从旧档**滑**到新档）
 *      + ② 判定路径流水线（notes 是有序因果链，命中节点逐级点亮、被拦节点灰掉下沉）
 *      + ③ 置信度环 + 依据列表（primary / muted 两档主次错开入场）。
 *
 * 只读：所有数据都来自 `router.updated` / 快照与配置快照，不在这里发请求。
 */
export function RouterPopover({ router, tiers, motion }: RouterPopoverProps) {
  const notes = describeNotes(router.notes)
  const tier = router.error ? '' : router.tier || ''
  const elapsed = typeof router.elapsed_ms === 'number' ? `${router.elapsed_ms} ms` : ''
  const load = router.load_ms ? `（含加载 ${router.load_ms} ms）` : ''
  const confidence = Math.max(0, Math.min(1, router.confidence ?? 0))
  const pipeline = buildPipeline(router.notes ?? [], router.score)

  // 滑块：换档发生时从旧档行滑到新档行（方案 14 §4.4）；打开浮层 / 同档时直接落位。
  const hlRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    const el = hlRef.current
    if (!el || !tier) return
    const endIdx = DESC.indexOf(tier)
    if (endIdx < 0) return
    const startIdx = motion.prevTier ? DESC.indexOf(motion.prevTier) : -1
    el.style.transition = 'none'
    if (startIdx < 0 || startIdx === endIdx) {
      el.style.transform = `translateY(${endIdx * ROW_H}px)`
      return
    }
    el.style.transform = `translateY(${startIdx * ROW_H}px)`
    const raf = requestAnimationFrame(() => {
      el.style.transition = ''
      el.style.transform = `translateY(${endIdx * ROW_H}px)`
    })
    return () => cancelAnimationFrame(raf)
  }, [tier, motion.prevTier, router.error])

  // 置信度环：从 0 扫到实际值；低置信改虚线（与"低置信被拒"语义对齐）。
  const ringRef = useRef<SVGCircleElement | null>(null)
  useEffect(() => {
    const el = ringRef.current
    if (!el) return
    el.style.transition = 'none'
    el.style.strokeDashoffset = String(RING_C)
    const raf = requestAnimationFrame(() => {
      el.style.transition = ''
      el.style.strokeDashoffset = String(RING_C * (1 - confidence))
    })
    return () => cancelAnimationFrame(raf)
  }, [confidence])

  const sortedTiers = [...tiers].sort((a, b) => tierIndex(b.name) - tierIndex(a.name))

  return (
    <div className="router-pop slide-in tiered" data-tier={tier || undefined}>
      <div className="router-pop-head">
        <span className="router-pop-title">本轮路由</span>
        <span className="router-pop-meta">{elapsed ? `${elapsed}${load}` : ''}</span>
      </div>

      {router.error ? <div className="router-pop-error">{router.error}</div> : null}

      {sortedTiers.length > 0 ? (
        <div className="track">
          {tier ? <div className="track-hl" ref={hlRef} /> : null}
          {sortedTiers.map((item) => {
            const isActive = Boolean(tier) && item.name === tier
            const resolvedProvider = item.resolved_provider || ''
            const resolvedModel = item.resolved_model || ''
            // 回落 = "实际会用"与"显式配置"不一致（未配置，或该 provider 缺 API Key）
            const fallsBack =
              Boolean(resolvedProvider || resolvedModel) &&
              (resolvedProvider !== item.provider || resolvedModel !== item.model)
            const provider = item.provider || resolvedProvider
            const model = item.model || resolvedModel
            return (
              <div
                key={item.name}
                className={`trow tiered${isActive ? ' on' : ''}${fallsBack ? ' fallback' : ''}`}
                data-tier={item.name}
              >
                <span className="tname">{item.name}</span>
                <TierLevel level={tierIndex(item.name) + 1} />
                <span className="tmodel">
                  {model || '—'}
                  {provider ? <span className="dim"> · {provider}</span> : null}
                  {fallsBack && resolvedModel && resolvedModel !== model ? (
                    <span className="dim"> → {resolvedModel}</span>
                  ) : null}
                </span>
                <span className="tflag">
                  {fallsBack ? (item.configured ? '已配置 · 回落' : '回落 active') : '已配置'}
                </span>
              </div>
            )
          })}
        </div>
      ) : (
        <div className="router-pop-section hint">正在读取档位配置…</div>
      )}

      <div className="router-pop-section">
        <div className="router-pop-title">本轮判定路径</div>
        <div className="pipe">
          {pipeline.map((node, index) => (
            <span key={node.name} className="pipe-cell">
              <span
                className={`pnode ${node.state}${node.state === 'off' ? '' : ' played'}`}
                style={{ animationDelay: node.state === 'off' ? undefined : `${120 + index * 80}ms` }}
              >
                <span className="cn">{node.name}</span>
                {node.val ? <span className="cv">{node.val}</span> : null}
              </span>
              {index < pipeline.length - 1 ? <span className="pipe-i">▶</span> : null}
            </span>
          ))}
        </div>
      </div>

      <div className="router-pop-section">
        <div className="router-pop-title">为什么是 {tier || '—'}</div>
        <div className="reason-wrap">
          <div className={`ring${confidence < 0.5 ? ' low' : ''}`}>
            <svg width="62" height="62" viewBox="0 0 62 62">
              <circle className="rtrack" cx="31" cy="31" r={RING_R} fill="none" strokeWidth="5" />
              <circle
                className="rbar"
                ref={ringRef}
                cx="31"
                cy="31"
                r={RING_R}
                fill="none"
                strokeWidth="5"
                strokeLinecap="round"
                strokeDasharray={RING_C.toFixed(2)}
                strokeDashoffset={RING_C.toFixed(2)}
              />
            </svg>
            <div className="rnum">
              {Math.round(confidence * 100)}%
              <small>置信度</small>
            </div>
          </div>
          <div className="reasons">
            {notes.length === 0 ? (
              <div className="hint">本轮没有可解释的记录（后端为旧版本，只下发档位）。</div>
            ) : (
              <ul className="router-notes">
                {notes.map((note, index) => (
                  <li
                    key={note.raw}
                    className={`${note.tone === 'muted' ? 'muted' : ''} played`}
                    style={{ animationDelay: `${520 + index * 80}ms` }}
                  >
                    <span className="mk">{note.tone === 'muted' ? '·' : '▪'}</span>
                    {note.label}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>

      <div className="router-pop-foot">
        {router.switched
          ? `本轮实际使用 ${router.provider || '—'} · ${router.model || '—'}（已切换）`
          : router.model
            ? `本轮沿用当前模型（目标 ${router.provider || '—'} · ${router.model}）`
            : '本轮未换模型'}
      </div>
    </div>
  )
}
