import type { WsEnvelope } from './types'

const API_PREFIX = '/api'

/**
 * 令牌来源优先级：URL 查询参数 → 构建期环境变量。
 *
 * 桌面壳每次启动生成随机令牌，并以 `?token=xxx` 打开窗口。必须在模块初始化时
 * 同步取到（不能等异步 IPC），否则首个 API 请求会 401。
 */
function readInitialToken(): string {
  try {
    const fromUrl = new URLSearchParams(window.location.search).get('token')
    if (fromUrl) return fromUrl
  } catch {
    // 非浏览器环境（如测试）忽略
  }
  return import.meta.env.VITE_ROUTIVUS_TOKEN ?? ''
}

let authToken: string = readInitialToken()

export function setAuthToken(token: string): void {
  authToken = token
}

export function getAuthToken(): string {
  return authToken
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string

  constructor(status: number, code: string, message: string, requestId = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
  }

  /** 服务端未启动 / 网络不可达时由 fetch 抛出，便于 UI 区分「连不上」。 */
  get isNetworkError(): boolean {
    return this.status === 0
  }
}

export function newRequestId(prefix = 'req'): string {
  const rand =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 12)
      : Math.random().toString(16).slice(2, 14)
  return `${prefix}-${rand}`
}

function buildHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra)
  headers.set('Accept', 'application/json')
  if (authToken) headers.set('Authorization', `Bearer ${authToken}`)
  return headers
}

async function parseError(response: Response): Promise<ApiError> {
  let code = `http_${response.status}`
  let message = response.statusText || '请求失败'
  const requestId = response.headers.get('X-Request-ID') ?? ''
  try {
    const body = (await response.json()) as {
      error?: { code?: string; message?: string; request_id?: string }
    }
    if (body?.error) {
      code = body.error.code ?? code
      message = body.error.message ?? message
    }
  } catch {
    // 非 JSON 错误体（例如网关返回的 HTML）沿用状态文本
  }
  return new ApiError(response.status, code, message, requestId)
}

interface RequestOptions {
  method?: string
  body?: unknown
  query?: Record<string, string | number | undefined | null>
  signal?: AbortSignal
}

function withQuery(path: string, query?: RequestOptions['query']): string {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const url = withQuery(`${API_PREFIX}${path}`, options.query)
  const headers = buildHeaders()
  const init: RequestInit = { method: options.method ?? 'GET', headers, signal: options.signal }
  if (options.body !== undefined) {
    headers.set('Content-Type', 'application/json')
    init.body = JSON.stringify(options.body)
  }

  let response: Response
  try {
    response = await fetch(url, init)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, 'network_error', '无法连接 Routivus Server，请确认服务已启动', '')
  }

  if (response.status === 204) return undefined as T
  if (!response.ok) throw await parseError(response)
  const text = await response.text()
  if (!text) return undefined as T
  return JSON.parse(text) as T
}

/** 构造 WebSocket 地址：同源 + /api 前缀 + 可选 token 查询参数。 */
export function wsUrl(path: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const base = `${protocol}//${window.location.host}${API_PREFIX}${path}`
  if (!authToken) return base
  const separator = base.includes('?') ? '&' : '?'
  return `${base}${separator}token=${encodeURIComponent(authToken)}`
}

export function isWsEnvelope(value: unknown): value is WsEnvelope {
  return typeof value === 'object' && value !== null && typeof (value as WsEnvelope).type === 'string'
}
