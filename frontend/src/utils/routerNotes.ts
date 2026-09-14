/**
 * SmartRouter 判定依据（`RouterState.notes`）的唯一文案出口（方案 08 §4.1/§4.6）。
 *
 * 后端只给**结构化枚举**（如 `hard_rule:arch`、`ml:idx=2,p=0.71`），中文文案集中
 * 在这里渲染——避免后端与前端各写一套、日后悄悄漂移。
 */
import type { RouterState } from '../api/types'

export const TIER_NAMES = ['Basic', 'Enhanced', 'Superior', 'Ultimate'] as const

export interface NoteView {
  /** 结构化原文（也用于 React key）。 */
  raw: string
  /** 中文短语。 */
  label: string
  /** 展示层级：primary 用于摘要，muted 是补充信息（ML 未采用、耗时之类）。 */
  tone: 'primary' | 'muted'
}

export function tierIndex(tier: string | undefined): number {
  return TIER_NAMES.indexOf((tier ?? '') as (typeof TIER_NAMES)[number])
}

function tierName(idx: number): string {
  return TIER_NAMES[idx] ?? `#${idx}`
}

export function formatScore(score: number): string {
  return Number.isInteger(score) ? String(score) : score.toFixed(1)
}

/**
 * ML 精判不可用的**可操作**文案（方案 10 §4.6）。
 *
 * 后端下发原因码（`ml:unavailable:<reason>`），这里翻成"该去装什么/查什么"，
 * 而不是一句合并的「无产物或依赖缺失」。
 */
function mlUnavailableLabel(reason: string): string {
  switch (reason) {
    case 'runtime_missing':
      return 'ML 精判不可用（onnxruntime 导入失败，重装 onnxruntime 即可恢复）'
    case 'artifact_missing':
      return 'ML 精判不可用（语义产物缺失）'
    case 'tokenizer_missing':
      return 'ML 精判不可用（语义产物缺伴生 tokenizer）'
    case 'dim_mismatch':
      return 'ML 精判不可用（语义产物输出维度不符）'
    case 'load_failed':
      return 'ML 精判不可用（语义产物加载失败）'
    case 'no_semantic':
      return 'ML 精判不可用（语义编码器不可用且无兜底产物）'
    case 'bad_artifact':
      return 'ML 精判不可用（产物损坏或格式不符）'
    case 'version_mismatch':
      return 'ML 精判不可用（产物版本与本版本不匹配）'
    case 'no_artifact':
      return 'ML 精判不可用（无产物）'
    case '':
      return 'ML 精判不可用（无产物或依赖缺失）'
    default:
      return `ML 精判不可用（原因码 ${reason}）`
  }
}

/** 单条依据 → 中文短语；未知格式原样返回，保证新枚举不会显示成空白。 */
export function describeNote(raw: string): NoteView {
  const text = String(raw ?? '')
  const [head, ...rest] = text.split(':')
  const tail = rest.join(':')

  switch (head) {
    case 'hard_rule':
      switch (tail) {
        case 'risk':
          return { raw: text, label: '风险词命中（硬规则 → 至少 Superior）', tone: 'primary' }
        case 'arch':
          return { raw: text, label: '强架构词命中（硬规则 → 至少 Superior）', tone: 'primary' }
        case 'code':
          return { raw: text, label: '代码块较多（硬规则 → Ultimate）', tone: 'primary' }
        case 'long':
          return { raw: text, label: '超长输入（硬规则 → Ultimate）', tone: 'primary' }
        case 'chatty':
          return { raw: text, label: '闲聊（硬规则 → Basic）', tone: 'primary' }
        default:
          return { raw: text, label: '硬规则直接决定档位', tone: 'primary' }
      }
    case 'score':
      return { raw: text, label: `规则总分 ${tail}`, tone: 'primary' }
    case 'ml': {
      if (tail.startsWith('idx=')) {
        const [idxPart, probPart] = tail.split(',')
        const idx = Number(idxPart.replace('idx=', ''))
        const prob = probPart?.replace('p=', '') ?? ''
        return {
          raw: text,
          label: `ML 精判 ${tierName(idx)}${prob ? `（p=${prob}）` : ''}`,
          tone: 'primary',
        }
      }
      if (tail.startsWith('skipped:low_conf')) {
        const prob = tail.replace('skipped:low_conf(', '').replace(')', '')
        return { raw: text, label: `ML 精判信心不足${prob ? `（${prob}）` : ''}，沿用规则档`, tone: 'muted' }
      }
      if (tail === 'nosem') {
        return {
          raw: text,
          label: 'ML 精判走无语义兜底版（该环境缺 onnxruntime，不影响路由）',
          tone: 'muted',
        }
      }
      if (tail === 'unavailable' || tail.startsWith('unavailable:')) {
        const reason = tail.startsWith('unavailable:') ? tail.slice('unavailable:'.length) : ''
        return { raw: text, label: mlUnavailableLabel(reason), tone: 'muted' }
      }
      return { raw: text, label: 'ML 精判异常，沿用规则档', tone: 'muted' }
    }
    case 'calibration': {
      const delta = Number(tail)
      const dir = delta > 0 ? '升' : '降'
      return { raw: text, label: `校准偏置${dir} ${Math.abs(delta)} 档`, tone: 'primary' }
    }
    case 'rule':
      switch (tail) {
        case 'risk':
          return { raw: text, label: '后处理：风险词 → 至少 Superior', tone: 'primary' }
        case 'long_context':
          return { raw: text, label: '后处理：长上下文 → 至少 Superior', tone: 'primary' }
        case 'arch':
          return { raw: text, label: '后处理：架构词 +1 档', tone: 'primary' }
        case 'debug':
          return { raw: text, label: '后处理：调试场景 +1 档', tone: 'primary' }
        case 'chatty':
          return { raw: text, label: '后处理：闲聊 → Basic', tone: 'primary' }
        default:
          return { raw: text, label: `后处理规则 ${tail}`, tone: 'primary' }
      }
    case 'learned': {
      const [deltaPart, predicate] = tail.split(':')
      const delta = Number(deltaPart)
      const dir = delta > 0 ? '+1' : '-1'
      return {
        raw: text,
        label: `自学习规则 ${dir} 档${predicate ? `（${predicate}）` : ''}`,
        tone: 'primary',
      }
    }
    case 'anti_downgrade':
      return {
        raw: text,
        label: `防降级：600s 内最多降一档${tail ? `（${tail.replace('→', ' → ')}）` : ''}`,
        tone: 'primary',
      }
    case 'hysteresis':
      return {
        raw: text,
        label: `迟滞：窗口内换档过多，冻结在 ${tail.replace('frozen:', '')}`,
        tone: 'primary',
      }
    default:
      return { raw: text, label: text, tone: 'muted' }
  }
}

export function describeNotes(notes: string[] | undefined): NoteView[] {
  return (notes ?? []).filter(Boolean).map(describeNote)
}

/** 一句话说明"为什么是这个档"：取第一条主要依据。 */
export function reasonSummary(notes: string[] | undefined): string {
  const views = describeNotes(notes)
  const primary = views.find((item) => item.tone === 'primary')
  return primary?.label ?? '按规则分档'
}

/** 顶栏 chip 文案（三态：失败 / 待路由 / 档位）。 */
export function routerChipLabel(router: RouterState | null): string {
  if (!router?.enabled) return ''
  if (router.error) return '⚡ 路由失败'
  if (!router.tier) return '⚡ 待路由'
  return `⚡ ${router.tier}`
}

/**
 * 对话流里的"换档提示"：只在**变化 / 回落 / 失败**，以及"其实被稳定层拦住了"
 * 这几种情况下返回文案，其余返回 null（不刷屏，方案 08 §4.4）。
 */
export function routerNotice(prev: RouterState | null, next: RouterState): string | null {
  if (!next.enabled) return null
  if (next.error) return `⚠ 路由失败，本轮沿用当前模型${next.error ? `（${next.error}）` : ''}`
  if (!next.tier) return null

  const previous = prev?.tier ?? ''
  const notes = next.notes ?? []

  if (next.configured === false) {
    const prefix = previous && previous !== next.tier ? `${previous} → ` : ''
    return `⚡ 档位回落 ${prefix}沿用 active（该档未显式配置）`
  }
  if (!previous) return `⚡ 本轮档位 ${next.tier} · ${reasonSummary(notes)}`
  if (previous !== next.tier) {
    const direction = tierIndex(next.tier) > tierIndex(previous) ? '上调' : '下调'
    return `⚡ 档位${direction} ${previous} → ${next.tier} · ${reasonSummary(notes)}`
  }
  // 档位没变，但要区分"本来就该这个档"和"被稳定层拦住了"
  if (notes.some((note) => note.startsWith('hysteresis:frozen'))) {
    return `⚡ 本轮沿用 ${next.tier}（迟滞窗口内换档过多，已冻结）`
  }
  if (notes.some((note) => note.startsWith('anti_downgrade'))) {
    return `⚡ 本轮沿用 ${next.tier}（防降级：600s 内最多降一档）`
  }
  return null
}
