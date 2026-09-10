import { useRef, useState } from 'react'
import type { SessionStatus } from '../../api/types'

interface ComposerProps {
  prompt: string
  value: string
  history: string[]
  status: SessionStatus | null
  disabled: boolean
  waitingApproval: boolean
  onChange: (value: string) => void
  onSubmit: () => void
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
  const inputRef = useRef<HTMLInputElement | null>(null)

  const busy = status === 'running' || status === 'waiting_approval'

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      if (!busy && !disabled && value.trim()) {
        onSubmit()
        setCursor(-1)
      }
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
      : '输入任务，Enter 发送，↑↓ 调历史'

  return (
    <div className="composer">
      <span className="composer-prompt">{prompt} &gt;</span>
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
          onClick={() => {
            onSubmit()
            setCursor(-1)
          }}
        >
          发送
        </button>
      )}
    </div>
  )
}
