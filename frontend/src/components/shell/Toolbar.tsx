import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'

/**
 * 顶栏工具条：按**优先级**决定"谁能留在外面"，装不下的区块收进「更多」下拉。
 *
 * 为什么不用媒体查询 / 容器查询：顶栏的实际可用宽度并不等于窗口宽度 ——
 * 左侧栏在 1180 / 1000 两个断点会变窄或隐藏、文件抽屉会占用右列、侧栏可收起，
 * 同一窗口宽度下顶栏可用宽度能差 300px 以上。断点只能按窗口宽度猜，猜不准；
 * 这里直接量顶栏自身宽度。
 *
 * 为什么不用 `overflow: hidden` 兜底：顶栏里挂着两个浮层（模型列表、路由依据），
 * 它们是绝对定位的子元素，裁掉容器会把浮层一起裁掉。
 */
export interface ToolbarSlot {
  /** 稳定标识：测量与收纳判定都按它索引 */
  key: string
  /** 越大越重要、越晚被收进「更多」；同值按声明顺序（排序是稳定的） */
  priority: number
  node: ReactNode
  /** 可压缩槽位：常驻不收纳，缩窄时靠内部的 `.tb-trunc` 出省略号 */
  flexible?: boolean
  /** 撑满顶栏高度（页签下划线要对齐底边） */
  stretch?: boolean
}

/** 弹性占位：把后面的区块推到最右；空间不够时先归零 */
export interface ToolbarSpacer {
  spacer: true
  key?: string
}

export type ToolbarItem = ToolbarSlot | ToolbarSpacer

/** 测量「更多」按钮宽度用的保留键（真实按钮只在有收纳时才渲染） */
const MORE_KEY = '__more'
/** 可压缩槽位至少留这么宽；连这也放不下就继续收纳别的区块 */
const MIN_FLEX_WIDTH = 96
/** 与 `.tb` / `.tb-measure` 的 column-gap 保持一致 */
const GAP = 14

function isSpacer(item: ToolbarItem): item is ToolbarSpacer {
  return 'spacer' in item && item.spacer === true
}

/**
 * 收纳决策（纯函数）：
 *
 * 1. 全部放得下 → 不收纳，也不渲染「更多」；
 * 2. 放不下 → 按优先级从高到低取"能塞进可用宽度的**最长前缀**"。
 *
 * 用前缀而不是"逐个填空"（小的先塞、大的跳过），是为了保证一个可解释的不变式：
 * **凡是进了「更多」的区块，优先级都低于留在外面的区块**。填空式会让"低优先级的
 * 小按钮留在外面、高优先级的大区块被收纳"，看起来像随机。
 */
function decideHidden(
  slots: ToolbarSlot[],
  widths: Record<string, number>,
  available: number,
  moreWidth: number,
): string[] {
  if (available <= 0) return []
  const flexSlots = slots.filter((slot) => slot.flexible)
  const rigidSlots = slots.filter((slot) => !slot.flexible)
  const flexReserve = flexSlots.reduce(
    (sum, slot) => sum + Math.min(MIN_FLEX_WIDTH, widths[slot.key] ?? 0),
    0,
  )
  const rigidWidth = rigidSlots.reduce((sum, slot) => sum + (widths[slot.key] ?? 0), 0)
  if (rigidWidth + flexReserve + GAP * Math.max(0, slots.length - 1) <= available) {
    return []
  }

  const ordered = [...rigidSlots].sort((a, b) => b.priority - a.priority)
  const kept = new Set<string>()
  // 预留两个 gap：收纳区前面的那个，以及「更多」与下一块之间
  let used = flexReserve + moreWidth + GAP * 2
  for (const slot of ordered) {
    const width = (widths[slot.key] ?? 0) + GAP
    if (used + width > available) break
    kept.add(slot.key)
    used += width
  }
  return ordered.filter((slot) => !kept.has(slot.key)).map((slot) => slot.key)
}

export function Toolbar({ items }: { items: ToolbarItem[] }) {
  const rootRef = useRef<HTMLDivElement | null>(null)
  const measureRef = useRef<HTMLDivElement | null>(null)
  const moreRef = useRef<HTMLDivElement | null>(null)
  const [hiddenKeys, setHiddenKeys] = useState<string[]>([])
  const [menuOpen, setMenuOpen] = useState(false)
  const hiddenRef = useRef('')

  const slots = items.filter((item): item is ToolbarSlot => !isSpacer(item))
  const slotsRef = useRef<ToolbarSlot[]>(slots)
  slotsRef.current = slots

  const recompute = useCallback(() => {
    const root = rootRef.current
    const measure = measureRef.current
    if (!root || !measure) return
    const available = root.clientWidth
    if (available <= 0) return // 未挂载 / 被 display:none 隐藏
    const widths: Record<string, number> = {}
    measure.querySelectorAll<HTMLElement>('[data-tb-key]').forEach((el) => {
      const key = el.dataset.tbKey
      if (key) widths[key] = el.offsetWidth
    })
    const next = decideHidden(slotsRef.current, widths, available, widths[MORE_KEY] ?? 0)
    const signature = next.join(',')
    // 用字符串比对挡住 setState：否则"渲染 → 测量 → setState → 渲染"会成环
    if (signature !== hiddenRef.current) {
      hiddenRef.current = signature
      setHiddenKeys(next)
    }
  }, [])

  // 宽度会随内容变化（模型名变长、笔记数 1 → 2…），所以每次渲染后都重量一次。
  // 放在 layout effect 里：同一帧内定稿，不会出现"先挤一下再收起"的闪动。
  useLayoutEffect(() => {
    recompute()
  })

  // 窗口缩放 / 侧栏收展不会触发 React 渲染，靠 ResizeObserver 补
  useEffect(() => {
    const root = rootRef.current
    if (!root || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => recompute())
    observer.observe(root)
    return () => observer.disconnect()
  }, [recompute])

  // 测量区里的按钮是"真的按钮"（同一份 JSX），用 inert 让它彻底不参与交互与 Tab 顺序。
  // 用 setAttribute 而不是 JSX 属性：React 18 的 `inert` 属性支持还不稳。
  useEffect(() => {
    const el = measureRef.current
    if (!el) return
    el.setAttribute('inert', '')
    return () => el.removeAttribute('inert')
  }, [])

  useEffect(() => {
    if (!menuOpen) return
    const onMouseDown = (event: MouseEvent) => {
      if (moreRef.current && !moreRef.current.contains(event.target as Node)) setMenuOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false)
    }
    window.addEventListener('mousedown', onMouseDown)
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('mousedown', onMouseDown)
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [menuOpen])

  // 收纳集合变了（比如窗口变宽放回来）就关掉菜单，免得浮层挂在一个已不存在的锚点上
  useEffect(() => {
    setMenuOpen(false)
  }, [hiddenKeys])

  const hiddenSet = new Set(hiddenKeys)
  const hiddenSlots = slots.filter((slot) => hiddenSet.has(slot.key))
  const firstHidden = hiddenSlots[0]?.key ?? ''

  const slotClass = (slot: ToolbarSlot) =>
    `tb-slot${slot.flexible ? ' tb-slot-flex' : ''}${slot.stretch ? ' tb-slot-stretch' : ''}`

  return (
    <div className="tb" ref={rootRef}>
      {items.map((item, index) => {
        if (isSpacer(item)) {
          return <span className="tb-spacer" key={item.key ?? `spacer-${index}`} />
        }
        if (hiddenSet.has(item.key)) {
          // 「更多」按钮渲染在第一个被收纳的区块的位置上
          if (item.key !== firstHidden) return null
          return (
            <div className="tb-more" key="tb-more" ref={moreRef}>
              <button
                type="button"
                className={`tb-more-btn${menuOpen ? ' open' : ''}`}
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                title={`还有 ${hiddenSlots.length} 项（窗口变宽会自动放回）`}
                onClick={() => setMenuOpen((open) => !open)}
              >
                ⋯
                <span className="tb-more-num">{hiddenSlots.length}</span>
              </button>
              {menuOpen ? (
                <div className="tb-more-pop" role="menu">
                  {hiddenSlots.map((slot) => (
                    <div className="tb-more-item" key={slot.key}>
                      {slot.node}
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          )
        }
        return (
          <div className={slotClass(item)} key={item.key} data-tb-slot={item.key}>
            {item.node}
          </div>
        )
      })}

      {/* 只用于测量：把全部槽位渲染一份到视口外。
          `visibility: hidden` + `pointer-events: none` 让它不可见、不可点；
          `inert`（在 effect 里加）再补一层，保证里面的按钮也进不了 Tab 顺序。 */}
      <div className="tb-measure" ref={measureRef} aria-hidden="true">
        {slots.map((slot) => (
          <div className={slotClass(slot)} key={slot.key} data-tb-key={slot.key}>
            {slot.node}
          </div>
        ))}
        <span className="tb-more-btn" data-tb-key={MORE_KEY}>
          ⋯<span className="tb-more-num">9</span>
        </span>
      </div>
    </div>
  )
}
