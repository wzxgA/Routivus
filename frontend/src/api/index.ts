import { authedUrl, request } from './client'
import type {
  ActivityDay,
  CompletionResponse,
  ConfigSnapshot,
  DesktopInfo,
  FileContent,
  FileListing,
  FileWriteResult,
  MaxTokensField,
  MemoryPayload,
  Message,
  ModelLimit,
  Note,
  NoteStats,
  Project,
  ProviderView,
  Session,
  SkillDetail,
  SkillView,
  SkillWritePayload,
  UsageSummary,
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

// ---- 长期记忆 ----

/** 会话所属项目的长期记忆条目（只读；命令改动后由侧栏重新拉取）。 */
export const fetchSessionMemory = (sessionId: string, limit = 20, signal?: AbortSignal) =>
  request<MemoryPayload>(`/sessions/${sessionId}/memory`, { query: { limit }, signal })

// ---- 项目工作区文件 ----

const fileApiPath = (projectId: string) => `/projects/${projectId}/file`

/** 列一层目录（逐层懒加载；`includeIgnored` 打开后才列出 .git / node_modules 等）。 */
export const listProjectFiles = (
  projectId: string,
  path = '',
  options?: { includeIgnored?: boolean; signal?: AbortSignal },
) =>
  request<FileListing>(`/projects/${projectId}/files`, {
    query: { path, include_ignored: options?.includeIgnored ? '1' : '' },
    signal: options?.signal,
  })

/** 读文本文件（二进制只给元信息；超限截断、解码失败标 lossy）。 */
export const readProjectFile = (projectId: string, path: string, signal?: AbortSignal) =>
  request<FileContent>(fileApiPath(projectId), { query: { path }, signal })

/**
 * 保存文件。`expectedVersion` 是打开时拿到的内容版本——服务端比对不上就回 409
 * `file_conflict`（绝不静默覆盖）；用户确认覆盖时传 `force`。
 */
export const writeProjectFile = (
  projectId: string,
  payload: { path: string; content: string; expectedVersion?: string; force?: boolean },
) =>
  request<FileWriteResult>(fileApiPath(projectId), {
    method: 'PUT',
    body: {
      path: payload.path,
      content: payload.content,
      expected_version: payload.expectedVersion ?? null,
      force: payload.force ?? false,
    },
  })

export const createProjectEntry = (
  projectId: string,
  payload: { path: string; kind: 'file' | 'dir' },
) =>
  request<{ path: string; type: string; created: boolean }>(
    `/projects/${projectId}/files`,
    { method: 'POST', body: payload },
  )

/**
 * 删除文件或目录（不可逆，服务端落审计）。
 *
 * 目录非空时必须显式传 `recursive`，否则服务端回 409 `directory_not_empty` —— 这是
 * 有意设计的"第二次确认"，避免手滑删掉整棵树。项目根、忽略目录（含 .git）与项目
 * 数据目录（.routivus）会被拒（422）。
 */
export const deleteProjectEntry = (
  projectId: string,
  payload: { path: string; recursive?: boolean },
) =>
  request<{ path: string; type: string; deleted: boolean; recursive: boolean }>(
    fileApiPath(projectId),
    {
      method: 'DELETE',
      query: { path: payload.path, recursive: payload.recursive ? '1' : '' },
    },
  )

/** 图片原始字节地址（`<img src>` 无法带 Authorization 头，走查询参数令牌）。 */
export const projectFileRawUrl = (projectId: string, path: string) =>
  authedUrl(`${fileApiPath(projectId)}/raw?path=${encodeURIComponent(path)}`)

// ---- 用量统计（方案 12）----

/**
 * 首页 token 面板：今日 / 区间 / 累计 + 按天 + 按项目 + 按档位。
 *
 * `days` 越界由服务端钳到 7–90（前端传错不该让首页空掉），所以这里不必自己校验。
 */
export const fetchUsageSummary = (days = 30, signal?: AbortSignal) =>
  request<UsageSummary>('/usage/summary', { query: { days }, signal })

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

/** 能力上限字段（创建 / 更新共用）；省略＝不改，model_limits 空对象＝清空覆盖。 */
export interface ProviderCapabilityPayload {
  context_window?: number | null
  max_output_tokens?: number | null
  max_tokens_field?: MaxTokensField | null
  model_limits?: Record<string, ModelLimit> | null
}

export const createProvider = (payload: {
  name: string
  api_base: string
  default_model: string
  display_name?: string | null
  api_key?: string | null
  set_base?: boolean
} & ProviderCapabilityPayload) =>
  request<ProviderView>('/config/providers', { method: 'POST', body: payload })

export const updateProvider = (
  name: string,
  payload: {
    api_base?: string
    default_model?: string
    display_name?: string | null
  } & ProviderCapabilityPayload,
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

export const getSkill = (name: string, projectId?: string) =>
  request<SkillDetail>(`/skills/${encodeURIComponent(name)}`, { query: { project_id: projectId } })

export const saveSkill = (name: string, payload: SkillWritePayload, projectId?: string) =>
  request<SkillDetail>(`/skills/${encodeURIComponent(name)}`, {
    method: 'PUT',
    body: payload,
    query: { project_id: projectId },
  })

// ---- 桌面端：运行时白名单授权 ----

export const getDesktopInfo = (signal?: AbortSignal) =>
  request<DesktopInfo>('/desktop/info', { signal })

export const grantWorkspaceRoot = (path: string) =>
  request<DesktopInfo>('/desktop/roots', { method: 'POST', body: { path } })
