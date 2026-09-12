import { useCallback, useEffect, useState } from 'react'
import * as api from '../../api'
import type { FileContent } from '../../api/types'
import { describeError } from '../../state/errors'
import { FilePreview } from './FilePreview'
import { FileTree } from './FileTree'

interface FilesDrawerProps {
  projectId: string | null
  open: boolean
  onRequestClose: () => void
  /** 跳到整页文件视图（抽屉里编辑体验差，编辑走那边）。 */
  onOpenInPage: (path: string | null) => void
}

/**
 * 聊天页右侧的文件抽屉：**只读**（方案 §3.2）。
 *
 * 用途是"边聊边瞄"——看 agent 在改哪些文件、确认某个路径长什么样；
 * 要编辑就点右上角跳到整页视图。
 */
export function FilesDrawer({
  projectId,
  open,
  onRequestClose,
  onOpenInPage,
}: FilesDrawerProps) {
  const [selected, setSelected] = useState<string | null>(null)
  const [file, setFile] = useState<FileContent | null>(null)
  const [error, setError] = useState('')

  // 关闭或换项目：清空状态，避免下次打开显示上一个项目的文件
  useEffect(() => {
    setSelected(null)
    setFile(null)
    setError('')
  }, [open, projectId])

  const openFile = useCallback(
    async (path: string) => {
      if (!projectId) return
      setSelected(path)
      setError('')
      try {
        setFile(await api.readProjectFile(projectId, path))
      } catch (caught) {
        setFile(null)
        setError(describeError(caught))
      }
    },
    [projectId],
  )

  // Esc 关闭（与终端抽屉一致的键盘习惯）
  useEffect(() => {
    if (!open) return
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onRequestClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onRequestClose])

  if (!open || !projectId) return null

  return (
    <aside className="files-drawer">
      <div className="fd-head">
        <span className="fd-title">项目文件</span>
        <span className="spacer" />
        <button
          type="button"
          className="link-btn"
          onClick={() => onOpenInPage(selected)}
          title="打开文件页（可编辑；抽屉只读）"
        >
          在文件页打开
        </button>
        <button type="button" className="link-btn" onClick={onRequestClose}>
          关闭
        </button>
      </div>

      <FileTree
        projectId={projectId}
        activePath={selected}
        onOpenFile={(path) => void openFile(path)}
        compact
      />

      <div className="fd-preview">
        {error ? (
          <div className="hint">{error}</div>
        ) : file ? (
          <FilePreview projectId={projectId} file={file} />
        ) : (
          <div className="hint">点上方文件查看内容（只读）。要编辑请点「编辑」。</div>
        )}
      </div>
    </aside>
  )
}
