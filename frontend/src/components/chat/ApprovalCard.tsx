import { useState } from 'react'
import type { ApprovalRequestedData } from '../../api/types'

interface ApprovalCardProps {
  approval: ApprovalRequestedData
  onResolve: (
    decision: 'approve' | 'reject',
    options?: { args?: Record<string, unknown>; scope?: 'session' },
  ) => void
  onAnswer: (answers: Record<string, string> | null) => void
}

const RISK_TEXT: Record<string, string> = {
  always: '高风险：该工具每次执行都需要审批',
  confirm: '中风险：该工具执行前需要确认',
}

function AskForm({
  approval,
  onAnswer,
}: {
  approval: ApprovalRequestedData
  onAnswer: (answers: Record<string, string> | null) => void
}) {
  const fields = approval.ask?.fields ?? []
  const [values, setValues] = useState<Record<string, string>>({})
  const [raw, setRaw] = useState('')

  const submit = () => {
    if (fields.length === 0) {
      const text = raw.trim()
      if (!text) {
        onAnswer({ answer: '' })
        return
      }
      try {
        const parsed = JSON.parse(text)
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          onAnswer(
            Object.fromEntries(
              Object.entries(parsed as Record<string, unknown>).map(([k, v]) => [k, String(v)]),
            ),
          )
          return
        }
      } catch {
        /* 非 JSON 时按纯文本回答 */
      }
      onAnswer({ answer: text })
      return
    }
    onAnswer(values)
  }

  return (
    <div className="approval">
      <div className="approval-title">
        <span className="approval-risk">提问</span>
        {approval.ask?.question ?? 'Agent 需要补充信息'}
      </div>
      {fields.length === 0 ? (
        <div className="ask-field">
          <div className="ask-label">回答（可填 JSON 对象，或纯文本）</div>
          <textarea
            className="ask-textarea"
            value={raw}
            onChange={(event) => setRaw(event.target.value)}
          />
        </div>
      ) : (
        fields.map((field, index) => {
          const name = field.name ?? `field_${index}`
          const label = field.label ?? name
          return (
            <div className="ask-field" key={name}>
              <div className="ask-label">
                {label}
                {field.required ? ' *' : ''}
              </div>
              {field.options && field.options.length ? (
                <select
                  className="ask-select"
                  value={values[name] ?? ''}
                  onChange={(event) =>
                    setValues((current) => ({ ...current, [name]: event.target.value }))
                  }
                >
                  <option value="">请选择</option>
                  {field.options.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  className="ask-input"
                  value={values[name] ?? ''}
                  placeholder={field.placeholder ?? ''}
                  onChange={(event) =>
                    setValues((current) => ({ ...current, [name]: event.target.value }))
                  }
                />
              )}
            </div>
          )
        })
      )}
      <div className="approval-actions">
        <button type="button" className="btn primary" onClick={submit}>
          提交回答
        </button>
        <button type="button" className="btn" onClick={() => onAnswer(null)}>
          跳过
        </button>
      </div>
    </div>
  )
}

export function ApprovalCard({ approval, onResolve, onAnswer }: ApprovalCardProps) {
  const [editing, setEditing] = useState(false)
  const [argsText, setArgsText] = useState(() =>
    JSON.stringify(approval.arguments ?? {}, null, 2),
  )
  const [parseError, setParseError] = useState<string | null>(null)

  if (approval.kind === 'ask') {
    return <AskForm approval={approval} onAnswer={onAnswer} />
  }

  const applyModified = () => {
    try {
      const parsed = JSON.parse(argsText)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setParseError('参数必须是 JSON 对象')
        return
      }
      setParseError(null)
      onResolve('approve', { args: parsed as Record<string, unknown> })
    } catch {
      setParseError('JSON 解析失败，请检查格式')
    }
  }

  return (
    <div className="approval">
      <div className="approval-title">
        <span className="approval-risk">需要审批</span>
        {approval.tool_name || '工具调用'}
        {approval.level ? <span className="approval-risk">{RISK_TEXT[approval.level] ?? approval.level}</span> : null}
      </div>

      {editing ? (
        <>
          <div className="ask-label">修改参数后执行（整体覆盖原参数）</div>
          <textarea
            className="ask-textarea"
            value={argsText}
            onChange={(event) => setArgsText(event.target.value)}
          />
          {parseError ? <div className="notes-conflict">{parseError}</div> : null}
        </>
      ) : (
        <div className="approval-args">{JSON.stringify(approval.arguments ?? {}, null, 2)}</div>
      )}

      <div className="approval-actions">
        {editing ? (
          <>
            <button type="button" className="btn primary" onClick={applyModified}>
              应用并批准
            </button>
            <button type="button" className="btn" onClick={() => setEditing(false)}>
              取消改参
            </button>
          </>
        ) : (
          <>
            <button type="button" className="btn primary" onClick={() => onResolve('approve')}>
              批准一次
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => onResolve('approve', { scope: 'session' })}
              title="本会话内同类工具不再请求审批；仍受 PathGuard / CommandGuard 约束"
            >
              本会话放行
            </button>
            <button type="button" className="btn" onClick={() => setEditing(true)}>
              改参执行
            </button>
            <button type="button" className="btn danger" onClick={() => onResolve('reject')}>
              拒绝
            </button>
          </>
        )}
      </div>
      {approval.timeout ? (
        <div className="approval-note">
          超时 {approval.timeout}s 未应答将按拒绝处理；策略层黑名单与越界路径始终拒绝。
        </div>
      ) : null}
    </div>
  )
}
