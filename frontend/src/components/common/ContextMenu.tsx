import { useEffect, useLayoutEffect, useRef, useState } from 'react'

export interface ContextMenuItem {
  key: string
  label: string
  /** 破坏性操作（删除…）：用危险色标出来，避免和普通菜单项混淆。 */
  danger?: boolean
  onSelect: () => void
}

interface ContextMenuProps {
  x: number
  y: number
  items: ContextMenuItem[]
  onClose: () => void
}

/**
 * 右键菜单：钉在鼠标坐标上，点空白 / Esc / 窗口滚动或缩放都会关掉。
 *
 * 不复用顶栏那个 `.pop`：它是锚在某个元素上的（相对父元素 `right: 0`），而右键菜单
 * 要贴在鼠标位置、还要在贴边时自己拉回视口内。
 */
export function ContextMenu({ x, y, items, onClose }: ContextMenuProps) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [pos, setPos] = useState({ left: x, top: y })

  // 先渲染再量尺寸，所以放 layout effect：贴边时把菜单推回视口内，避免被裁掉
  useLayoutEffect(() => {
    const element = ref.current
    if (!element) return
    const { width, height } = element.getBoundingClientRect()
    const margin = 6
    setPos({
      left: Math.max(margin, Math.min(x, window.innerWidth - width - margin)),
      top: Math.max(margin, Math.min(y, window.innerHeight - height - margin)),
    })
  }, [x, y])

  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    const onViewportChange = () => onClose()
    window.addEventListener('mousedown', onPointerDown)
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('resize', onViewportChange)
    // capture：滚动可能发生在内层容器（会话列表 / 文件树）里
    window.addEventListener('scroll', onViewportChange, true)
    return () => {
      window.removeEventListener('mousedown', onPointerDown)
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('resize', onViewportChange)
      window.removeEventListener('scroll', onViewportChange, true)
    }
  }, [onClose])

  return (
    <div
      className="ctx-menu"
      role="menu"
      ref={ref}
      style={{ left: pos.left, top: pos.top }}
      // 菜单上再右键不该弹出系统菜单（与条目一致）
      onContextMenu={(event) => event.preventDefault()}
    >
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          role="menuitem"
          className={`ctx-item${item.danger ? ' danger' : ''}`}
          onClick={() => {
            onClose()
            item.onSelect()
          }}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}
