import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from '../../api'
import { ApiError } from '../../api/client'
import type { FileContent, FileWriteResult } from '../../api/types'
import { describeError } from '../../state/errors'
import { FilePreview } from './FilePreview'

/** 预览上限：超过就不渲染（避免一次预览把大文件整份解析成元素树）。 */
const PREVIEW_CHAR_LIMIT = 200_000

interface FileEditorProps {
  projectId: string
  file: FileContent
  /** 保存成功后通知外层（外层会重新读文件并把树标记为需要刷新）。 */
  onSaved: (result: FileWriteResult) => void
  /** 放弃改动 / 冲突后重新加载。 */
  onReload: () => void
}

interface Cursor {
  line: number
  column: number
}

function cursorAt(text: string, index: number): Cursor {
  const upTo = text.slice(0, index)
  const lastBreak = upTo.lastIndexOf('\n')
  return { line: upTo.split('\n').length, column: index - lastBreak }
}

/**
 * 文本编辑器（P0 用 textarea，见方案 §3.3）。
 *
 * 保存语义：带上打开时的内容版本，服务端比对不上就回 409 `file_conflict`——
 * 这里把冲突渲染成选择条（覆盖 / 重新加载），**不静默覆盖**。
 * 预览开关渲染的是**当前草稿**（不是磁盘内容），便于保存前先看一眼效果。
 */
export function FileEditor({ projectId, file, onSaved, onReload }: FileEditorProps) {
  const [draft, setDraft] = useState(file.content)
  const [saving, setSaving] = useState(false)
  const [conflict, setConflict] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [cursor, setCursor] = useState<Cursor>({ line: 1, column: 1 })
  const [preview, setPreview] = useState(false)
  const areaRef = useRef<HTMLTextAreaElement | null>(null)
  const noticeTimer = useRef<number | null>(null)

  // 换文件 / 保存后重新加载：用服务端内容重置草稿与提示
  useEffect(() => {
    setDraft(file.content)
    setConflict(false)
    setError('')
  }, [file.content, file.path, file.version])

  // 切换文件时回到编辑态（同一组件实例会被复用）
  useEffect(() => {
    setPreview(false)
    setCursor({ line: 1, column: 1 })
  }, [file.path])

  useEffect(
    () => () => {
      if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current)
    },
    [],
  )

  const dirty = draft !== file.content

  const syncCursor = useCallback(() => {
    const area = areaRef.current
    if (!area) return
    setCursor(cursorAt(area.value, area.selectionStart))
  }, [])

  const save = useCallback(
    async (force = false) => {
      if (saving) return
      setSaving(true)
      setError('')
      try {
        const result = await api.writeProjectFile(projectId, {
          path: file.path,
          content: draft,
          expectedVersion: file.version,
          force,
        })
        setConflict(false)
        setNotice(result.created ? '已创建' : '已保存')
        if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current)
        noticeTimer.current = window.setTimeout(() => setNotice(''), 1500)
        onSaved(result)
      } catch (caught) {
        if (caught instanceof ApiError && caught.code === 'file_conflict') {
          setConflict(true)
        } else {
          setError(describeError(caught))
        }
      } finally {
        setSaving(false)
      }
    },
    [draft, file.path, file.version, onSaved, projectId, saving],
  )

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
      event.preventDefault()
      void save()
      return
    }
    if (event.key === 'Tab') {
      // 文本域里 Tab 默认跳焦点，编辑器里应插入缩进
      event.preventDefault()
      const area = areaRef.current
      if (!area) return
      const start = area.selectionStart
      const end = area.selectionEnd
      setDraft(`${draft.slice(0, start)}  ${draft.slice(end)}`)
      requestAnimationFrame(() => {
        area.setSelectionRange(start + 2, start + 2)
        syncCursor()
      })
    }
  }

  const tooLargeForPreview = draft.length > PREVIEW_CHAR_LIMIT

  return (
    <div className="file-editor">
      <div className="fe-bar">
        <span className="fe-path" title={file.path}>
          {file.path}
        </span>
        <span className="spacer" />
        <button
          type="button"
          className={`btn tiny${preview ? ' primary' : ''}`}
          onClick={() => setPreview((value) => !value)}
          title={preview ? '回到编辑' : '渲染预览（内容是当前草稿，不是磁盘版本）'}
          disabled={!preview && tooLargeForPreview}
        >
          {preview ? '编辑' : '预览'}
        </button>
        <button
          type="button"
          className="btn tiny"
          disabled={!dirty}
          onClick={onReload}
          title="丢弃当前改动，重新读取磁盘内容"
        >
          放弃改动
        </button>
        <button
          type="button"
          className="btn tiny primary"
          disabled={!dirty || saving}
          onClick={() => void save()}
        >
          {saving ? '保存中…' : '保存'}
        </button>
      </div>

      {conflict ? (
        <div className="banner">
          <span>文件已被外部修改，直接保存会覆盖别的改动。</span>
          <span className="spacer" />
          <button type="button" className="link-btn" onClick={() => void save(true)}>
            用我的内容覆盖
          </button>
          <button type="button" className="link-btn" onClick={onReload}>
            重新加载
          </button>
        </div>
      ) : null}
      {error ? (
        <div className="banner error">
          <span>{error}</span>
        </div>
      ) : null}

      {preview ? (
        tooLargeForPreview ? (
          <div className="fe-preview fe-preview-large">
            内容较大（{Math.round(draft.length / 1024)}KB），预览已关闭以避免卡顿；保存后可在只读视图查看。
          </div>
        ) : (
          <div className="fe-preview">
            <FilePreview projectId={projectId} file={{ ...file, content: draft }} />
          </div>
        )
      ) : (
        <textarea
          ref={areaRef}
          className="fe-area"
          value={draft}
          spellCheck={false}
          onChange={(event) => {
            setDraft(event.target.value)
            syncCursor()
          }}
          onKeyDown={handleKeyDown}
          onKeyUp={syncCursor}
          onClick={syncCursor}
          onSelect={syncCursor}
        />
      )}

      <div className="fe-status">
        行 {cursor.line}，列 {cursor.column} · {file.line_ending === 'crlf' ? 'CRLF' : 'LF'} ·{' '}
        {(file.size / 1024).toFixed(1)} KB
        {dirty ? ' · 未保存' : ''}
        {notice ? ` · ${notice}` : ''}
      </div>
    </div>
  )
}
