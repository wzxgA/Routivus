import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from '../api'
import type { Note, Project, Session } from '../api/types'
import { describeError } from './workspace'

export interface ProjectWorkspaceValue {
  project: Project | null
  sessions: Session[]
  activeSession: Session | null
  projectNotes: Note[]
  loading: boolean
  error: string | null
  refresh: () => Promise<void>
  refreshNotes: (query?: string) => Promise<void>
  createSession: () => Promise<Session | null>
  renameSession: (sessionId: string, title: string) => Promise<void>
  removeSession: (sessionId: string) => Promise<void>
  applySessionUpdate: (session: Session) => void
}

/**
 * 项目态数据聚合：项目详情、会话列表、项目范围笔记。
 *
 * `sessionId` 来自路由。未指定时默认选中最近更新的会话；项目内一个会话都没有
 * 时会自动创建一个，保证会话视图始终有可操作的目标。
 */
export function useProjectWorkspace(
  projectId: string | null,
  sessionId: string | null,
): ProjectWorkspaceValue {
  const [project, setProject] = useState<Project | null>(null)
  const [sessions, setSessions] = useState<Session[]>([])
  const [projectNotes, setProjectNotes] = useState<Note[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const autoCreated = useRef(false)

  const refresh = useCallback(async () => {
    if (!projectId) {
      setProject(null)
      setSessions([])
      setProjectNotes([])
      return
    }
    setLoading(true)
    try {
      const [detail, sessionList, notes] = await Promise.all([
        api.getProject(projectId),
        api.listSessions(projectId),
        api.listProjectNotes(projectId, ''),
      ])
      setProject(detail)
      setSessions(sessionList)
      setProjectNotes(notes)
      setError(null)
    } catch (err) {
      setError(describeError(err))
      setProject(null)
      setSessions([])
      setProjectNotes([])
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    autoCreated.current = false
    void refresh()
  }, [refresh])

  // 无会话时自动创建一个，避免项目态会话视图空白无从下手。
  useEffect(() => {
    if (!projectId || loading || autoCreated.current) return
    if (sessions.length === 0 && project) {
      autoCreated.current = true
      api
        .createSession(projectId, '新会话')
        .then((session) => setSessions([session]))
        .catch((err) => setError(describeError(err)))
    }
  }, [projectId, loading, sessions, project])

  const refreshNotes = useCallback(
    async (query = '') => {
      if (!projectId) return
      try {
        setProjectNotes(await api.listProjectNotes(projectId, query))
      } catch (err) {
        setError(describeError(err))
      }
    },
    [projectId],
  )

  const createSession = useCallback(async () => {
    if (!projectId) return null
    try {
      const session = await api.createSession(projectId, '新会话')
      setSessions((current) => [session, ...current])
      return session
    } catch (err) {
      setError(describeError(err))
      return null
    }
  }, [projectId])

  const renameSession = useCallback(async (targetId: string, title: string) => {
    try {
      const updated = await api.updateSession(targetId, title)
      setSessions((current) => current.map((s) => (s.id === targetId ? updated : s)))
    } catch (err) {
      setError(describeError(err))
    }
  }, [])

  const removeSession = useCallback(async (targetId: string) => {
    try {
      await api.deleteSession(targetId)
      setSessions((current) => current.filter((s) => s.id !== targetId))
    } catch (err) {
      setError(describeError(err))
    }
  }, [])

  const applySessionUpdate = useCallback((session: Session) => {
    setSessions((current) => {
      const exists = current.some((s) => s.id === session.id)
      if (!exists) return [session, ...current]
      return current.map((s) => (s.id === session.id ? session : s))
    })
  }, [])

  const activeSession =
    (sessionId ? sessions.find((s) => s.id === sessionId) : undefined) ?? sessions[0] ?? null

  return {
    project,
    sessions,
    activeSession,
    projectNotes,
    loading,
    error,
    refresh,
    refreshNotes,
    createSession,
    renameSession,
    removeSession,
    applySessionUpdate,
  }
}
