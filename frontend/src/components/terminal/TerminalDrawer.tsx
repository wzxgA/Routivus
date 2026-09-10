import { useCallback, useEffect, useRef, useState } from 'react'
import type { WsEnvelope } from '../../api/types'
import { TerminalSocket, type TerminalState } from '../../ws/terminalSocket'

const ANSI_PATTERN = [
  // CSI 序列
  /\u001b\[[0-9;?]*[ -/]*[@-~]/g,
  // OSC 序列（含窗口标题）
  /\u001b\][^\u0007]*(\u0007|\u001b\\)/g,
  // 其他双字符转义
  /\u001b[@-Z\\-_]/g,
].map((pattern) => pattern)

const MAX_BUFFER = 200_000

function stripAnsi(input: string): string {
  let text = input
  for (const pattern of ANSI_PATTERN) text = text.replace(pattern, '')
  return text.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
}

interface TerminalDrawerProps {
  projectId: string | null
  open: boolean
  cwd: string | null
  onRequestClose: () => void
}

/**
 * 终端抽屉：与会话 WebSocket 独立的项目终端通道。
 *
 * 后端强制要求鉴权（Token 或 Origin 白名单），否则以 `terminal_auth_required`
 * 拒绝；缺少 pywinpty 且后端为 auto 时返回 `terminal_unavailable`。两种情况都会
 * 在终端正文区给出可读提示。
 */
export function TerminalDrawer({ projectId, open, cwd, onRequestClose }: TerminalDrawerProps) {
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const socketRef = useRef<TerminalSocket | null>(null)
  const bufferRef = useRef('')
  const [output, setOutput] = useState('')
  const [state, setState] = useState<TerminalState>('idle')
  const [message, setMessage] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [history, setHistory] = useState<string[]>([])
  const [cursor, setCursor] = useState(-1)

  const appendOutput = useCallback((chunk: string) => {
    bufferRef.current = (bufferRef.current + chunk).slice(-MAX_BUFFER)
    setOutput(bufferRef.current)
  }, [])

  const estimateCols = useCallback(() => {
    const width = bodyRef.current?.clientWidth ?? 900
    return Math.max(40, Math.floor(width / 7.2))
  }, [])

  const estimateRows = useCallback(() => {
    const height = bodyRef.current?.clientHeight ?? 180
    return Math.max(6, Math.floor(height / 17))
  }, [])

  useEffect(() => {
    if (!open || !projectId) {
      socketRef.current?.close()
      socketRef.current = null
      return
    }
    const socket = new TerminalSocket(projectId, {
      onState: (next, detail) => {
        setState(next)
        if (detail) setMessage(detail)
        if (next === 'error') setMessage(detail ?? '终端连接失败')
      },
      onEvent: (event: WsEnvelope) => {
        const data = (event.data ?? {}) as Record<string, unknown>
        switch (event.type) {
          case 'terminal.opened': {
            setMessage(null)
            setState('open')
            const backend = String(data.backend ?? '')
            appendOutput(`[terminal] backend=${backend} cwd=${String(data.cwd ?? cwd ?? '')}\n`)
            return
          }
          case 'terminal.output': {
            const raw = String(data.data ?? '')
            let decoded = raw
            if (String(data.encoding ?? 'utf8') === 'base64') {
              try {
                decoded = atob(raw)
              } catch {
                decoded = raw
              }
            }
            appendOutput(stripAnsi(decoded))
            return
          }
          case 'terminal.output.dropped': {
            appendOutput(`\n[terminal] 输出背压丢弃 ${String(data.count ?? '?')} 块\n`)
            return
          }
          case 'terminal.exit': {
            appendOutput(`\n[terminal] 进程退出 code=${String(data.exit_code ?? '?')}\n`)
            return
          }
          case 'terminal.closed': {
            appendOutput(`\n[terminal] 已关闭 (${String(data.reason ?? '')})\n`)
            setState('closed')
            return
          }
          case 'error': {
            setMessage(String(event.message ?? event.code ?? '终端错误'))
            return
          }
          default:
            return
        }
      },
    })
    socketRef.current = socket
    socket.open(estimateCols(), estimateRows())
    return () => {
      socket.close()
      socketRef.current = null
    }
    // cwd 仅用于提示，不参与 socket 生命周期
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, projectId, appendOutput])

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight
  }, [output])

  const submit = () => {
    const socket = socketRef.current
    if (!socket) return
    const command = input
    setInput('')
    setCursor(-1)
    if (command.trim()) setHistory((current) => [...current, command])
    if (/^(clear|cls)$/i.test(command.trim())) {
      bufferRef.current = ''
      setOutput('')
      socket.clear()
      return
    }
    appendOutput(`${cwd ?? ''}> ${command}\n`)
    socket.sendInput(`${command}\r\n`)
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      submit()
      return
    }
    if (event.key === 'ArrowUp') {
      if (history.length === 0) return
      event.preventDefault()
      const next = cursor === -1 ? history.length - 1 : Math.max(0, cursor - 1)
      setCursor(next)
      setInput(history[next])
      return
    }
    if (event.key === 'ArrowDown') {
      if (history.length === 0 || cursor === -1) return
      event.preventDefault()
      const next = cursor + 1
      if (next >= history.length) {
        setCursor(-1)
        setInput('')
      } else {
        setCursor(next)
        setInput(history[next])
      }
    }
  }

  return (
    <div className={`term${open ? ' open' : ''}`}>
      <div className="term-head">
        <span className="term-title">cmd</span>
        <span className="term-cwd" title={cwd ?? ''}>
          {cwd ?? ''}
        </span>
        <span className="term-acts">
          <span style={{ fontSize: 10, color: 'var(--term-faint)', marginRight: 2 }}>{state}</span>
          <button
            type="button"
            className="term-act"
            onClick={() => {
              bufferRef.current = ''
              setOutput('')
              socketRef.current?.clear()
            }}
          >
            清空
          </button>
          <button type="button" className="term-act" onClick={onRequestClose}>
            收起
          </button>
        </span>
      </div>
      <div className="term-body" ref={bodyRef}>
        {message ? <div className="term-out err">{message}</div> : null}
        <div className="term-out">{output}</div>
        <div className="term-input-line">
          <span className="term-ps">{cwd ? `${cwd}>` : '>'}</span>
          <input
            className="term-input"
            value={input}
            disabled={state !== 'open'}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={state === 'open' ? '' : '终端未就绪'}
          />
        </div>
      </div>
    </div>
  )
}
