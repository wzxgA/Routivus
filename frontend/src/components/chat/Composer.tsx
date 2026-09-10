import { useEffect, useRef, useState } from 'react'
import type { SessionStatus } from '../../api/types'

/** 发送模式：与后端 `/plan`、`/team` 前缀一一对应（见 `_parse_task_command`）。 */
export type ComposerMode = 'chat' | 'plan' | 'team'

const MODES: { id: ComposerMode; label: string; hint: string }[] = [
  { id: 'chat', label: '对话', hint: '直接执行，不生成计划' },
  { id: 'plan', label: '计划', hint: '先生成依赖图，审阅后按批次执行' },
  { id: 'team', label: '团队', hint: '多 Agent 协作，结果带证据化审查' },
]

const MODE_LABEL: Record<ComposerMode, string> = {
  chat: '对话',
  plan: '计划',
  team: '团队',
}

interface ComposerProps {
  prompt: string
  value: string
  history: string[]
  status: SessionStatus | null
  disabled: boolean
  waitingApproval: boolean
  onChange: (value: string) => void
  onSubmit: (mode: ComposerMode) => void
  onCancel: () => void
}

export function Composer({
  prompt,
  value,
  history,
  status,
  disabled,
  waitingApproval,
  onChange,
  onSubmit,
  onCancel,
}: ComposerProps) {
  const [cursor, setCursor] = useState(-1)
  const [mode, setMode] = useState<ComposerMode>('chat')
  const [menuOpen, setMenuOpen] = useState(false)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const modeRef = useRef<HTMLDivElement | null>(null)

  const busy = status === 'running' || status === 'waiting_approval'

  // 点菜单外部关闭（与顶栏模型选择器同构）
  useEffect(() => {
    if (!menuOpen) return
    const handler = (event: MouseEvent) => {
      if (modeRef.current && !modeRef.current.contains(event.target as Node)) setMenuOpen(false)
    }
    window.addEventListener('mousedown', handler)
    return () => window.removeEventListener('mousedown', handler)
  }, [menuOpen])

  // Composer 不可用时不要让菜单悬着
  useEffect(() => {
    if (disabled || busy) setMenuOpen(false)
  }, [disabled, busy])

  const submit = () => {
    if (busy || disabled || !value.trim()) return
    // 模式单次生效：交给上层包装成 /plan、/team 后立即回到对话模式
    onSubmit(mode)
    setMode('chat')
    setMenuOpen(false)
    setCursor(-1)
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
      return
    }
    if (event.key === 'ArrowUp') {
      if (history.length === 0) return
      event.preventDefault()
      const next = cursor === -1 ? history.length - 1 : Math.max(0, cursor - 1)
      setCursor(next)
      onChange(history[next])
      return
    }
    if (event.key === 'ArrowDown') {
      if (history.length === 0 || cursor === -1) return
      event.preventDefault()
      const next = cursor + 1
      if (next >= history.length) {
        setCursor(-1)
        onChange('')
      } else {
        setCursor(next)
        onChange(history[next])
      }
    }
  }

  const placeholder = waitingApproval
    ? '等待审批决策…（在上方审批卡中选择动作）'
    : busy
      ? 'Agent 正在执行…'
      : mode === 'plan'
        ? '描述要规划的任务，Enter 发送（计划模式）'
        : mode === 'team'
          ? '描述要交给多个 Agent 的任务，Enter 发送（团队模式）'
          : '输入任务，Enter 发送，↑↓ 调历史'

  return (
    <div className="composer">
      <span className="composer-prompt">{prompt} &gt;</span>

      <div className={`pmode${menuOpen ? ' open' : ''}`} ref={modeRef}>
        <button
          type="button"
          className={`pmode-btn${mode === 'chat' ? '' : ' active'}`}
          disabled={disabled || busy}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          title="选择发送模式"
          onClick={() => setMenuOpen((open) => !open)}
        >
          <span>{MODE_LABEL[mode]}</span>
          <span className="pmode-caret">▼</span>
        </button>
        <div className="pmode-pop" role="menu">
          <div className="pop-title">发送模式</div>
          {MODES.map((item) => (
            <button
              type="button"
              key={item.id}
              role="menuitemradio"
              aria-checked={mode === item.id}
              className="pmode-item"
              onClick={() => {
                setMode(item.id)
                setMenuOpen(false)
                inputRef.current?.focus()
              }}
            >
              <span className="pmode-name">{item.label}</span>
              <span className="pmode-desc">{item.hint}</span>
              {mode === item.id ? <span className="pmode-tick">●</span> : null}
            </button>
          ))}
        </div>
      </div>

      <input
        ref={inputRef}
        value={value}
        placeholder={placeholder}
        disabled={disabled || busy}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
      />
      {busy ? (
        <button type="button" className="btn tiny" onClick={onCancel}>
          停止
        </button>
      ) : (
        <button
          type="button"
          className="btn tiny primary"
          disabled={disabled || !value.trim()}
          onClick={submit}
        >
          发送
        </button>
      )}
    </div>
  )
}
