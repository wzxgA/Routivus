import { useCallback, useState, type MouseEvent as ReactMouseEvent } from 'react'

export interface ContextMenuState<T> {
  x: number
  y: number
  target: T
}

/**
 * 右键菜单的开合状态：`open(target, event)` 记下目标与鼠标坐标，`close()` 收起。
 *
 * 与 `ContextMenu` 组件配套（会话列表、文件树共用）。坐标只在打开那一刻取自事件，
 * 之后不跟随鼠标——菜单必须钉在原地，跟着动反而不好点。
 */
export function useContextMenu<T>() {
  const [menu, setMenu] = useState<ContextMenuState<T> | null>(null)

  const open = useCallback((target: T, event: ReactMouseEvent) => {
    event.preventDefault()
    event.stopPropagation()
    setMenu({ x: event.clientX, y: event.clientY, target })
  }, [])

  const close = useCallback(() => setMenu(null), [])

  return { menu, open, close }
}
