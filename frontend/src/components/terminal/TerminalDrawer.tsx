import { useCallback, useEffect, useRef, useState } from 'react'
import type { TerminalEnvelope } from '../../api/types'
import { TerminalSocket, type TerminalState } from '../../ws/terminalSocket'

/* eslint-disable no-control-regex -- 剥离 ANSI 转义序列必须匹配控制字符（ESC \x1b、BEL \x07） */
const ANSI_PATTERN = [
  // CSI 序列
  /\u001b\[[0-9;?]*[ -/]*[@-~]/g,
  // OSC 序列（含窗口标题）
  /\u001b\][^\u0007]*(\u0007|\u001b\\)/g,
  // 其他双字符转义
  /\u001b[@-Z\\-_]/g,
]
/* eslint-enable no-control-regex */

const MAX_BUFFER = 200_000

function stripAnsi(input: string): string {
  let text = input
  for (const pattern of ANSI_PATTERN) text = text.replace(pattern, '')
  return text.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
}

/** shell 可执行文件名（去路径、去 .exe、小写）：`C:\...\pwsh.exe` → `pwsh`。 */
function shellName(shell: string): string {
  const file = shell.replace(/\\/g, '/').split('/').pop() ?? ''
  return file.replace(/\.exe$/i, '').trim().toLowerCase()
}

/** 标题短标签：powershell / pwsh 统一显示成 PowerShell。 */
function shellLabel(shell: string): string {
  const name = shellName(shell)
  if (!name) return 'terminal'
  return name === 'powershell' || name === 'pwsh' ? 'PowerShell' : name
}

/**
 * 输入框宽度（列）：CJK 等全角字符记 2 列，其余记 1 列。
 *
 * 输入框是内联在输出流末尾（提示符后面）的，宽度必须跟着已输入内容走，
 * 否则光标不会停在提示符正后方，或者会平白把行撑开。
 */
function inputColumns(text: string): number {
  let cols = 0
  for (const ch of text) cols += ch.charCodeAt(0) > 0x2e80 ? 2 : 1
  return Math.max(1, cols + 1)
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
  const inputRef = useRef<HTMLInputElement | null>(null)
  const bufferRef = useRef('')
  const [output, setOutput] = useState('')
  const [state, setState] = useState<TerminalState>('idle')
  const [message, setMessage] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [history, setHistory] = useState<string[]>([])
  const [cursor, setCursor] = useState(-1)
  // 服务端实际启动的 shell（配置项 ROUTIVUS_TERMINAL_SHELL 决定），随
  // terminal.opened 到达；标题与提示符按它显示，不再硬编码成 cmd。
  const [shell, setShell] = useState('')
  // 后端类型（conpty / oneshot）：决定要不要补本地回显 —— ConPTY 下 shell 自己
  // 会回显，再打一遍就是重复；oneshot 没有提示符与回显，只能靠本地补。
  const [backend, setBackend] = useState('')
  const promptText = /^(powershell|pwsh)$/.test(shellName(shell))
    ? cwd
      ? `PS ${cwd}>`
      : 'PS>'
    : cwd
      ? `${cwd}>`
      : '>'
  // ConPTY 下 shell 自己会把提示符（含真实 cwd）打进输出流，抽屉再画一遍就成了
  // 两条路径；输入框改为内联在输出流末尾 —— 通常正好落在提示符后面，像真终端
  // 那样可以直接在当前路径后输入。oneshot 没有提示符，才需要补一个前缀。

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
      onEvent: (event: TerminalEnvelope) => {
        switch (event.type) {
          case 'terminal.opened': {
            setMessage(null)
            setState('open')
            setShell(typeof event.shell === 'string' ? event.shell : '')
            setBackend(event.backend ?? '')
            // 不往输出流里塞信息行：真终端打开就是提示符。shell / backend / cwd
            // 都改在表头显示（标题 + 右侧路径 + 状态旁的后端名）。
            return
          }
          case 'terminal.output': {
            const raw = event.data ?? ''
            let decoded = raw
            if (event.encoding === 'base64') {
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
            appendOutput(`\n[terminal] 输出背压丢弃 ${event.count ?? '?'} 块\n`)
            return
          }
          case 'terminal.exit': {
            appendOutput('\n[terminal] 进程已退出\n')
            return
          }
          case 'terminal.closed': {
            const code =
              event.exit_code === undefined || event.exit_code === null
                ? ''
                : ` code=${event.exit_code}`
            appendOutput(`\n[terminal] 已关闭 (${event.reason ?? ''}${code})\n`)
            setState('closed')
            return
          }
          case 'error': {
            setMessage(event.message ?? event.code ?? '终端错误')
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

  // 终端就绪即聚焦：光标落在提示符后面，直接敲字就能用（和真终端一样）
  useEffect(() => {
    if (state === 'open') inputRef.current?.focus()
  }, [state])

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
    // 只发 CR：Windows 控制台里「回车」就是 CR。发 `\r\n` 时多出的那个 LF 在
    // PowerShell 里是「插入换行」而不是提交，会留下一个 `>>` 续行状态，把下一条
    // 命令并进上一条的续行（实测：PowerShell 收到 CRLF 会多一个 `>>`，只发 CR 时
    // PowerShell 与 cmd 都干净）。
    // 本地回显只给不自己回显的后端补（oneshot 没有提示符；ConPTY 下 PowerShell
    // 与 cmd 都会把提示符 + 命令回显出来）。
    if (backend !== 'conpty') appendOutput(`${promptText} ${command}\n`)
    socket.sendInput(`${command}\r`)
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
        <span className="term-title">{shellLabel(shell)}</span>
        <span className="term-cwd" title={cwd ?? ''}>
          {cwd ?? ''}
        </span>
        <span className="term-acts">
          <span style={{ fontSize: 10, color: 'var(--term-faint)', marginRight: 2 }}>
            {backend ? `${state} · ${backend}` : state}
          </span>
          <button
            type="button"
            className="term-act"
            onClick={() => {
              bufferRef.current = ''
              setOutput('')
              socketRef.current?.clear()
              inputRef.current?.focus()
            }}
          >
            清空
          </button>
          <button type="button" className="term-act" onClick={onRequestClose}>
            收起
          </button>
        </span>
      </div>
      <div
        className="term-body"
        ref={bodyRef}
        onMouseUp={() => {
          // 点正文把焦点还给输入框（真终端就是这样）；有选中文本时不抢，免得打断复制
          if (!window.getSelection()?.toString()) inputRef.current?.focus()
        }}
      >
        {message ? <div className="term-out err">{message}</div> : null}
        <div className="term-out">
          {output}
          {/* 输入框内联在输出流末尾：通常正好落在提示符（PS D:\x>）后面，光标因此停在
              当前路径后，和真终端一致。oneshot 没有提示符，补一个前缀。 */}
          {backend === 'conpty' ? null : <span className="term-ps">{promptText} </span>}
          <input
            ref={inputRef}
            className="term-input"
            value={input}
            disabled={state !== 'open'}
            style={{ width: `${inputColumns(input)}ch` }}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={handleKeyDown}
          />
        </div>
      </div>
    </div>
  )
}
