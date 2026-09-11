import { request } from './client'
import type {
  ActivityDay,
  CompletionResponse,
  ConfigSnapshot,
  DesktopInfo,
  Message,
  Note,
  NoteStats,
  Project,
  ProviderView,
  Session,
  SkillView,
} from './types'

// ---- 项目 ----

export const listProjects = (signal?: AbortSignal) =>
  request<Project[]>('/projects', { signal })

export const getProject = (projectId: string) => request<Project>(`/projects/${projectId}`)

export const createProject = (payload: { name: string; root_path: string }) =>
  request<Project>('/projects', { method: 'POST', body: payload })

export const updateProject = (
  projectId: string,
  payload: { name?: string; root_path?: string },
) => request<Project>(`/projects/${projectId}`, { method: 'PATCH', body: payload })

export const deleteProject = (projectId: string) =>
  request<void>(`/projects/${projectId}`, { method: 'DELETE' })

// ---- 活动 ----

export const listActivity = (from?: string, to?: string, signal?: AbortSignal) =>
  request<ActivityDay[]>('/activity', { query: { from, to }, signal })

// ---- 会话 ----

export const listSessions = (projectId: string, signal?: AbortSignal) =>
  request<Session[]>(`/projects/${projectId}/sessions`, { query: { limit: 50 }, signal })

export const getCurrentSession = (projectId: string) =>
  request<Session | null>(`/projects/${projectId}/sessions/current`)

export const createSession = (projectId: string, title = '新建会话') =>
  request<Session>(`/projects/${projectId}/sessions`, { method: 'POST', body: { title } })

export const getSession = (sessionId: string) => request<Session>(`/sessions/${sessionId}`)

export const updateSession = (sessionId: string, title: string) =>
  request<Session>(`/sessions/${sessionId}`, { method: 'PATCH', body: { title } })

export const deleteSession = (sessionId: string) =>
  request<void>(`/sessions/${sessionId}`, { method: 'DELETE' })

export const listMessages = (sessionId: string, limit = 200) =>
  request<Message[]>(`/sessions/${sessionId}/messages`, { query: { limit } })

export const listSessionEvents = (sessionId: string, after: number, limit = 500) =>
  request<unknown[]>(`/sessions/${sessionId}/events`, { query: { after, limit } })

export const cancelSession = (sessionId: string) =>
  request<{ cancelled: boolean }>(`/sessions/${sessionId}/cancel`, { method: 'POST' })

// ---- 命令补全 ----

const COMPLETIONS_PATH = '/completions'

export const fetchCompletions = (
  q: string,
  cursor: number,
  sessionId: string | null,
  signal?: AbortSignal,
) =>
  request<CompletionResponse>(COMPLETIONS_PATH, {
    query: { q, cursor, session_id: sessionId ?? undefined },
    signal,
  })

// ---- 笔记 ----

export const listNotes = (query: string, signal?: AbortSignal) =>
  request<Note[]>('/notes', { query: { scope: 'global', query, limit: 200 }, signal })

export const noteStats = () => request<NoteStats>('/notes/stats')

export const createNote = (payload: {
  title: string
  body_markdown?: string
  tags?: string[]
  project_id?: string | null
}) => request<Note>('/notes', { method: 'POST', body: payload })

export const getNote = (noteId: string) => request<Note>(`/notes/${noteId}`)

export const updateNote = (
  noteId: string,
  payload: {
    title?: string
    body_markdown?: string
    tags?: string[]
    project_id?: string | null
    version?: number
  },
) => request<Note>(`/notes/${noteId}`, { method: 'PATCH', body: payload })

export const deleteNote = (noteId: string) =>
  request<void>(`/notes/${noteId}`, { method: 'DELETE' })

export const pinNote = (noteId: string, pinned?: boolean) =>
  request<Note>(`/notes/${noteId}/pin`, { method: 'POST', body: pinned === undefined ? {} : { pinned } })

// ---- 项目范围笔记（服务端强制 project_id 隔离） ----

export const listProjectNotes = (projectId: string, query: string, signal?: AbortSignal) =>
  request<Note[]>(`/projects/${projectId}/notes`, { query: { query, limit: 200 }, signal })

export const createProjectNote = (
  projectId: string,
  payload: { title: string; body_markdown?: string; tags?: string[] },
) => request<Note>(`/projects/${projectId}/notes`, { method: 'POST', body: payload })

export const updateProjectNote = (
  projectId: string,
  noteId: string,
  payload: { title?: string; body_markdown?: string; tags?: string[]; version?: number },
) => request<Note>(`/projects/${projectId}/notes/${noteId}`, { method: 'PATCH', body: payload })

export const deleteProjectNote = (projectId: string, noteId: string) =>
  request<void>(`/projects/${projectId}/notes/${noteId}`, { method: 'DELETE' })

export const pinProjectNote = (projectId: string, noteId: string, pinned?: boolean) =>
  request<Note>(`/projects/${projectId}/notes/${noteId}/pin`, {
    method: 'POST',
    body: pinned === undefined ? {} : { pinned },
  })

// ---- 配置：Provider / SmartRouter ----

const providerPath = (name: string) => `/config/providers/${encodeURIComponent(name)}`

export const getConfig = (signal?: AbortSignal) =>
  request<ConfigSnapshot>('/config', { signal })

export const createProvider = (payload: {
  name: string
  api_base: string
  default_model: string
  display_name?: string | null
  api_key?: string | null
  set_base?: boolean
}) => request<ProviderView>('/config/providers', { method: 'POST', body: payload })

export const updateProvider = (
  name: string,
  payload: { api_base?: string; default_model?: string; display_name?: string | null },
) => request<ProviderView>(providerPath(name), { method: 'PATCH', body: payload })

export const deleteProvider = (name: string) =>
  request<void>(providerPath(name), { method: 'DELETE' })

export const setProviderKey = (name: string, apiKey: string) =>
  request<ProviderView>(`${providerPath(name)}/key`, {
    method: 'POST',
    body: { api_key: apiKey, overwrite: true },
  })

export const addProviderModel = (name: string, model: string) =>
  request<ProviderView>(`${providerPath(name)}/models`, { method: 'POST', body: { model } })

export const removeProviderModel = (name: string, model: string) =>
  request<ProviderView>(`${providerPath(name)}/models`, { method: 'DELETE', query: { model } })

export const setActiveProvider = (provider: string, model?: string) =>
  request<ConfigSnapshot>('/config/active', { method: 'POST', body: { provider, model } })

export const setTier = (tier: string, provider: string, model?: string) =>
  request<ConfigSnapshot>(`/config/tiers/${encodeURIComponent(tier)}`, {
    method: 'PUT',
    body: { provider, model },
  })

export const clearTier = (tier: string) =>
  request<ConfigSnapshot>(`/config/tiers/${encodeURIComponent(tier)}`, { method: 'DELETE' })

export const setSmartRouter = (enabled: boolean) =>
  request<ConfigSnapshot>('/config/smart-router', { method: 'POST', body: { enabled } })

// ---- Skill（只读列表 + 启停） ----

export const listSkills = (projectId?: string, signal?: AbortSignal) =>
  request<SkillView[]>('/skills', { query: { project_id: projectId }, signal })

export const setSkillEnabled = (name: string, enabled: boolean, projectId?: string) =>
  request<SkillView[]>(`/skills/${encodeURIComponent(name)}/${enabled ? 'enable' : 'disable'}`, {
    method: 'POST',
    query: { project_id: projectId },
  })

// ---- 桌面端：运行时白名单授权 ----

export const getDesktopInfo = (signal?: AbortSignal) =>
  request<DesktopInfo>('/desktop/info', { signal })

export const grantWorkspaceRoot = (path: string) =>
  request<DesktopInfo>('/desktop/roots', { method: 'POST', body: { path } })
