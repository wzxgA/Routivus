import { isWsEnvelope, wsUrl } from '../api/client'
import type { WsEnvelope } from '../api/types'

export type ConnState = 'connecting' | 'connected' | 'reconnecting' | 'offline'

const BACKOFF_MS = [800, 1600, 3200, 6400, 12000, 15000]
const CLIENT_PING_MS = 25_000

export interface SessionSocketHandlers {
  onEvent: (event: WsEnvelope) => void
  onState: (state: ConnState) => void
}

/**
 * 会话 WebSocket 客户端：负责连接生命周期、心跳、断线重连与序号去重。
 *
 * 序号策略：每次（重）连服务端都会下发一条 `session.snapshot`，其 sequence 是
 * 单调递增的。收到 snapshot 时重置本地 lastSequence；此后只接受 sequence 严格
 * 递增的事件，从而避免重连窗口内重复回放造成消息翻倍。events 接口可按
 * `after=lastSequence` 补齐缺口（由 ChatView 触发）。
 */
export class SessionSocket {
  private socket: WebSocket | null = null
  private handlers: SessionSocketHandlers
  private projectId: string
  private sessionId: string

  private intentionalClose = false
  private attempt = 0
  private lastSequence = 0
  private reconnectTimer: number | null = null
  private pingTimer: number | null = null

  constructor(projectId: string, sessionId: string, handlers: SessionSocketHandlers) {
    this.projectId = projectId
    this.sessionId = sessionId
    this.handlers = handlers
  }

  get sequence(): number {
    return this.lastSequence
  }

  get isOpen(): boolean {
    return this.socket?.readyState === WebSocket.OPEN
  }

  open(): void {
    this.intentionalClose = false
    this.connect()
  }

  close(): void {
    this.intentionalClose = true
    this.clearTimers()
    const socket = this.socket
    this.socket = null
    if (socket) {
      socket.onopen = null
      socket.onmessage = null
      socket.onclose = null
      socket.onerror = null
      try {
        socket.close()
      } catch {
        /* 忽略关闭异常 */
      }
    }
    this.handlers.onState('offline')
  }

  send(payload: Record<string, unknown>): boolean {
    if (!this.isOpen || !this.socket) return false
    this.socket.send(JSON.stringify(payload))
    return true
  }

  private connect(): void {
    this.handlers.onState(this.attempt === 0 ? 'connecting' : 'reconnecting')
    const path = `/ws/projects/${encodeURIComponent(this.projectId)}/sessions/${encodeURIComponent(
      this.sessionId,
    )}`
    let socket: WebSocket
    try {
      socket = new WebSocket(wsUrl(path))
    } catch {
      this.scheduleReconnect()
      return
    }
    this.socket = socket

    socket.onopen = () => {
      this.attempt = 0
      this.handlers.onState('connected')
      this.startClientPing()
    }

    socket.onmessage = (event) => {
      let parsed: unknown
      try {
        parsed = JSON.parse(String(event.data))
      } catch {
        return
      }
      if (!isWsEnvelope(parsed)) return
      const envelope = parsed as WsEnvelope

      if (envelope.type === 'ping') {
        this.send({ type: 'pong' })
        return
      }
      if (envelope.type === 'pong') return

      if (envelope.type === 'session.snapshot') {
        this.lastSequence = envelope.sequence ?? 0
        this.handlers.onEvent(envelope)
        return
      }
      if (typeof envelope.sequence === 'number') {
        if (envelope.sequence <= this.lastSequence) return
        this.lastSequence = envelope.sequence
      }
      this.handlers.onEvent(envelope)
    }

    socket.onclose = () => {
      this.clearPing()
      this.socket = null
      if (this.intentionalClose) {
        this.handlers.onState('offline')
        return
      }
      this.scheduleReconnect()
    }

    socket.onerror = () => {
      // onerror 之后浏览器必然触发 onclose，重连逻辑统一放在 onclose。
    }
  }

  private scheduleReconnect(): void {
    if (this.intentionalClose) return
    this.handlers.onState('reconnecting')
    const delay = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)]
    this.attempt += 1
    this.clearTimer()
    this.reconnectTimer = window.setTimeout(() => {
      if (!this.intentionalClose) this.connect()
    }, delay)
  }

  private startClientPing(): void {
    this.clearPing()
    this.pingTimer = window.setInterval(() => {
      this.send({ type: 'ping' })
    }, CLIENT_PING_MS)
  }

  private clearPing(): void {
    if (this.pingTimer !== null) {
      window.clearInterval(this.pingTimer)
      this.pingTimer = null
    }
  }

  private clearTimer(): void {
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private clearTimers(): void {
    this.clearTimer()
    this.clearPing()
  }
}
