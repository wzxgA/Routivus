import { isWsEnvelope, newRequestId, wsUrl } from '../api/client'
import type { WsEnvelope } from '../api/types'

export type TerminalState = 'idle' | 'connecting' | 'open' | 'closed' | 'error'

export interface TerminalHandlers {
  onEvent: (event: WsEnvelope) => void
  onState: (state: TerminalState, detail?: string) => void
}

const CLIENT_PING_MS = 25_000

/**
 * 项目终端 WebSocket 客户端。
 *
 * 与会话 socket 相互独立（终端生命周期与 Agent 轮次无关）。第一条消息必须是
 * `terminal.open`；服务端会回复 `terminal.opened`（含 cwd/shell/backend）或一条
 * error（如 `terminal_unavailable`、`terminal_auth_required`）。
 */
export class TerminalSocket {
  private socket: WebSocket | null = null
  private handlers: TerminalHandlers
  private projectId: string
  private pingTimer: number | null = null
  private intentionalClose = false

  constructor(projectId: string, handlers: TerminalHandlers) {
    this.projectId = projectId
    this.handlers = handlers
  }

  get isOpen(): boolean {
    return this.socket?.readyState === WebSocket.OPEN
  }

  open(cols = 120, rows = 30): void {
    this.intentionalClose = false
    this.handlers.onState('connecting')
    const path = `/ws/projects/${encodeURIComponent(this.projectId)}/terminal`
    let socket: WebSocket
    try {
      socket = new WebSocket(wsUrl(path))
    } catch {
      this.handlers.onState('error', '无法建立终端连接')
      return
    }
    this.socket = socket

    socket.onopen = () => {
      this.send({ type: 'terminal.open', request_id: newRequestId('term'), cols, rows })
      this.startPing()
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
      if (envelope.type === 'terminal.opened') this.handlers.onState('open')
      if (envelope.type === 'terminal.closed') this.handlers.onState('closed')
      if (envelope.type === 'error') {
        this.handlers.onState('error', envelope.message ?? envelope.code)
      }
      this.handlers.onEvent(envelope)
    }

    socket.onclose = () => {
      this.clearPing()
      this.socket = null
      if (!this.intentionalClose) this.handlers.onState('closed')
    }

    socket.onerror = () => {
      /* 统一由 onclose 收尾 */
    }
  }

  send(payload: Record<string, unknown>): boolean {
    if (!this.isOpen || !this.socket) return false
    this.socket.send(JSON.stringify(payload))
    return true
  }

  sendInput(data: string): void {
    this.send({ type: 'terminal.input', request_id: newRequestId('tin'), data })
  }

  resize(cols: number, rows: number): void {
    this.send({ type: 'terminal.resize', request_id: newRequestId('trs'), cols, rows })
  }

  clear(): void {
    this.send({ type: 'terminal.clear', request_id: newRequestId('tcl') })
  }

  close(): void {
    this.intentionalClose = true
    if (this.isOpen) this.send({ type: 'terminal.close', request_id: newRequestId('tx') })
    this.clearPing()
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
        /* 忽略 */
      }
    }
    this.handlers.onState('closed')
  }

  private startPing(): void {
    this.clearPing()
    this.pingTimer = window.setInterval(() => this.send({ type: 'ping' }), CLIENT_PING_MS)
  }

  private clearPing(): void {
    if (this.pingTimer !== null) {
      window.clearInterval(this.pingTimer)
      this.pingTimer = null
    }
  }
}
