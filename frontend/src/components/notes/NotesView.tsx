import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as api from '../../api'
import { ApiError } from '../../api/client'
import type { Note } from '../../api/types'
import { describeError } from '../../state/workspace'
import { countWords, formatDateTime, formatRelative } from '../../utils/format'
import { Empty } from '../common/Empty'
import { Modal } from '../common/Modal'

export type NoteScope = 'global' | 'project'

interface NotesViewProps {
  scope: NoteScope
  projectId: string | null
  projectName: string | null
  selectedNoteId: string | null
  onSelectNote: (noteId: string | null) => void
  onMutated: () => void
}

interface Draft {
  title: string
  body: string
  tags: string
}

function tagsToText(tags: string[]): string {
  return tags.join(', ')
}

function textToTags(text: string): string[] {
  return Array.from(
    new Set(
      text
        .split(/[,，]/)
        .map((value) => value.trim())
        .filter(Boolean),
    ),
  )
}

export function NotesView({
  scope,
  projectId,
  projectName,
  selectedNoteId,
  onSelectNote,
  onMutated,
}: NotesViewProps) {
  const [notes, setNotes] = useState<Note[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft>({ title: '', body: '', tags: '' })
  const [saving, setSaving] = useState(false)
  const [conflict, setConflict] = useState(false)
  const [editorError, setEditorError] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<Note | null>(null)
  const titleRef = useRef<HTMLInputElement | null>(null)

  // 切换范围时清空搜索，避免沿用旧范围关键词造成误判（原型与计划均要求）。
  useEffect(() => {
    setQuery('')
  }, [scope, projectId])

  const load = useCallback(
    async (search: string) => {
      setLoading(true)
      try {
        const result =
          scope === 'project'
            ? projectId
              ? await api.listProjectNotes(projectId, search)
              : []
            : await api.listNotes(search)
        setNotes(result)
        setListError(null)
      } catch (err) {
        setListError(describeError(err))
        setNotes([])
      } finally {
        setLoading(false)
      }
    },
    [scope, projectId],
  )

  useEffect(() => {
    const timer = window.setTimeout(() => void load(query), query ? 250 : 0)
    return () => window.clearTimeout(timer)
  }, [load, query])

  const selected = useMemo(
    () => notes.find((note) => note.id === selectedNoteId) ?? null,
    [notes, selectedNoteId],
  )

  // 列表加载后保证有选中项；选中项被删除时回落到第一条。
  useEffect(() => {
    if (notes.length === 0) {
      if (selectedNoteId !== null) onSelectNote(null)
      return
    }
    if (!selectedNoteId || !notes.some((note) => note.id === selectedNoteId)) {
      onSelectNote(notes[0].id)
    }
  }, [notes, selectedNoteId, onSelectNote])

  useEffect(() => {
    if (!selected) {
      setDraft({ title: '', body: '', tags: '' })
      setConflict(false)
      return
    }
    setDraft({
      title: selected.title,
      body: selected.body_markdown,
      tags: tagsToText(selected.tags),
    })
    setConflict(false)
    setEditorError(null)
  }, [selected])

  const dirty = useMemo(() => {
    if (!selected) return false
    return (
      draft.title !== selected.title ||
      draft.body !== selected.body_markdown ||
      draft.tags !== tagsToText(selected.tags)
    )
  }, [draft, selected])

  const persist = useCallback(
    async (note: Note, force: boolean) => {
      setSaving(true)
      setEditorError(null)
      const payload = {
        title: draft.title.trim(),
        body_markdown: draft.body,
        tags: textToTags(draft.tags),
        ...(force ? {} : { version: note.version }),
      }
      try {
        if (scope === 'project' && projectId) {
          await api.updateProjectNote(projectId, note.id, payload)
        } else {
          await api.updateNote(note.id, payload)
        }
        setConflict(false)
        await load(query)
        onMutated()
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          setConflict(true)
          setEditorError('笔记已在其他位置被修改，保存被拒绝。')
        } else {
          setEditorError(describeError(err))
        }
      } finally {
        setSaving(false)
      }
    },
    [draft, scope, projectId, load, query, onMutated],
  )

  const handleSave = useCallback(
    async (force = false) => {
      if (!selected || !draft.title.trim()) return
      await persist(selected, force)
    },
    [selected, draft.title, persist],
  )

  const handleCreate = useCallback(async () => {
    const payload = { title: '未命名笔记', body_markdown: '', tags: [] as string[] }
    try {
      const created =
        scope === 'project' && projectId
          ? await api.createProjectNote(projectId, payload)
          : await api.createNote({ ...payload, project_id: null })
      await load(query)
      onSelectNote(created.id)
      onMutated()
      window.setTimeout(() => titleRef.current?.focus(), 0)
    } catch (err) {
      setListError(describeError(err))
    }
  }, [scope, projectId, load, query, onSelectNote, onMutated])

  const handlePin = useCallback(async () => {
    if (!selected) return
    try {
      if (scope === 'project' && projectId) {
        await api.pinProjectNote(projectId, selected.id)
      } else {
        await api.pinNote(selected.id)
      }
      await load(query)
      onMutated()
    } catch (err) {
      setEditorError(describeError(err))
    }
  }, [selected, scope, projectId, load, query, onMutated])

  const handleDelete = useCallback(async () => {
    if (!pendingDelete) return
    try {
      if (scope === 'project' && projectId) {
        await api.deleteProjectNote(projectId, pendingDelete.id)
      } else {
        await api.deleteNote(pendingDelete.id)
      }
      setPendingDelete(null)
      onSelectNote(null)
      await load(query)
      onMutated()
    } catch (err) {
      setEditorError(describeError(err))
      setPendingDelete(null)
    }
  }, [pendingDelete, scope, projectId, load, query, onSelectNote, onMutated])

  const scopeLabel = scope === 'global' ? '全局笔记' : `${projectName ?? '项目'}笔记`

  return (
    <section className="view notes-view">
      <div className="notes-side">
        <div className="notes-toolbar">
          <input
            value={query}
            placeholder={scope === 'global' ? '搜索标题、#标签或项目名' : '搜索标题、#标签'}
            onChange={(event) => setQuery(event.target.value)}
          />
          <button type="button" className="btn" onClick={handleCreate}>
            新建笔记
          </button>
        </div>
        {listError ? (
          <div className="banner error" style={{ margin: 8 }}>
            {listError}
          </div>
        ) : null}
        <div className="notes-list">
          {notes.map((note) => (
            <button
              type="button"
              key={note.id}
              className={`note-item${note.id === selectedNoteId ? ' active' : ''}`}
              onClick={() => onSelectNote(note.id)}
            >
              <div className="note-top">
                <span className="note-title">{note.title}</span>
                {note.pinned ? <span className="pin">置顶</span> : null}
              </div>
              {scope === 'global' ? (
                <div className="note-meta">{note.project_name ?? '全局'}</div>
              ) : null}
              <div className="note-prev">{note.preview || '（空笔记）'}</div>
              <div className="note-meta">
                {formatRelative(note.updated_at)}
                {note.tags.length ? ` · ${note.tags.map((tag) => `#${tag}`).join(' ')}` : ''}
              </div>
            </button>
          ))}
          {!loading && notes.length === 0 ? (
            <div className="notes-empty">
              {query ? '没有匹配的笔记' : scope === 'project' ? '暂无关联笔记' : '还没有笔记'}
            </div>
          ) : null}
        </div>
      </div>

      {selected ? (
        <div className="notes-editor">
          <div className="ed-top">
            <input
              ref={titleRef}
              className="ed-title"
              value={draft.title}
              onChange={(event) => setDraft((d) => ({ ...d, title: event.target.value }))}
            />
            <span className="ed-meta">
              归属 {scope === 'global' ? (selected.project_name ?? '全局') : (projectName ?? '项目')}
            </span>
          </div>
          <div className="ed-meta">
            更新 {formatDateTime(selected.updated_at)} · {countWords(draft.body)} 字
            {selected.tags.length ? ` · ${selected.tags.map((t) => `#${t}`).join(' ')}` : ''}
          </div>
          <div className="ed-tags">
            <span>标签</span>
            <input
              value={draft.tags}
              placeholder="逗号分隔，例如：架构, 待办"
              onChange={(event) => setDraft((d) => ({ ...d, tags: event.target.value }))}
            />
          </div>
          <textarea
            className="ed-body"
            value={draft.body}
            placeholder="Markdown 正文…"
            onChange={(event) => setDraft((d) => ({ ...d, body: event.target.value }))}
          />
          <div className="ed-actions">
            {editorError ? <span className="notes-conflict">{editorError}</span> : null}
            {conflict ? (
              <button
                type="button"
                className="btn danger"
                disabled={saving}
                onClick={() => void handleSave(true)}
              >
                用当前内容覆盖
              </button>
            ) : null}
            <button type="button" className="btn" onClick={handlePin}>
              {selected.pinned ? '取消置顶' : '置顶'}
            </button>
            <button
              type="button"
              className="btn danger"
              onClick={() => setPendingDelete(selected)}
            >
              删除
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={!dirty || saving || !draft.title.trim()}
              onClick={() => void handleSave(false)}
            >
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        </div>
      ) : (
        <div className="notes-editor">
          <Empty
            title={scopeLabel}
            hint={query ? '没有匹配的笔记' : '选择左侧笔记，或新建一条。'}
            actionLabel="新建笔记"
            onAction={handleCreate}
          />
        </div>
      )}

      {pendingDelete ? (
        <Modal
          title="删除笔记"
          description={`将永久删除「${pendingDelete.title}」。此操作不可撤销，且只影响这条笔记。`}
          onClose={() => setPendingDelete(null)}
          actions={
            <>
              <button type="button" className="btn" onClick={() => setPendingDelete(null)}>
                取消
              </button>
              <button type="button" className="btn danger" onClick={() => void handleDelete()}>
                删除
              </button>
            </>
          }
        >
          <div />
        </Modal>
      ) : null}
    </section>
  )
}
