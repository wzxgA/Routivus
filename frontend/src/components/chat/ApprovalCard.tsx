import { useState } from 'react'
import type { ApprovalRequestedData } from '../../api/types'

/** 下拉里「自定义…」的哨兵值；选中时展示手填输入框，提交取其内容。 */
const CUSTOM = '__custom__'

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
  const ask = approval.ask
  const prompt = ask?.prompt ?? ''
  const fields = ask?.fields ?? []
  // 每个字段的当前选择：选项取值 / '__custom__'（自定义）/ ''（未选）
  const [values, setValues] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {}
    for (const field of fields) {
      const matched = (field.options ?? []).find((option) => option.value === field.default)
      initial[field.key] = matched ? matched.value : field.default ? CUSTOM : ''
    }
    return initial
  })
  // 自定义输入的内容（allow_custom 的下拉分支与无选项的纯文本字段共用）
  const [customs, setCustoms] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {}
    for (const field of fields) {
      const matched = (field.options ?? []).some((option) => option.value === field.default)
      if (field.default && !matched) initial[field.key] = field.default
    }
    return initial
  })
  const [raw, setRaw] = useState('')
  const [missing, setMissing] = useState<string[]>([])

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
    // 答案必须按 field.key 回填：模型是按 key 取值的（react.py 里 json.dumps 后回灌）
    const answers: Record<string, string> = {}
    const missingQuestions: string[] = []
    for (const field of fields) {
      const choice = values[field.key] ?? ''
      let value = ''
      if (choice === CUSTOM) {
        value = (customs[field.key] ?? '').trim()
      } else if (choice) {
        value = choice
      } else if (!(field.options ?? []).length) {
        // 无选项的纯文本字段：输入框里的内容就是答案
        value = (customs[field.key] ?? '').trim()
      }
      if (!value && field.required) missingQuestions.push(field.question || field.key)
      answers[field.key] = value
    }
    setMissing(missingQuestions)
    if (missingQuestions.length > 0) return
    onAnswer(answers)
  }

  return (
    <div className="approval">
      <div className="approval-title">
        <span className="approval-risk">提问</span>
        {prompt || 'Agent 需要补充信息'}
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
        fields.map((field) => {
          const choice = values[field.key] ?? ''
          // value 缺省等于 label（AskOption 语义）：数据类直构时可能为空串，
          // 渲染与提交都做兜底，避免空 value 与「请选择」占位项撞车。
          const options = (field.options ?? []).map((option) => ({
            ...option,
            value: option.value || option.label,
          }))
          return (
            <div className="ask-field" key={field.key}>
              <div className="ask-label">
                {field.question || field.key}
                {field.required ? ' *' : ''}
              </div>
              {options.length > 0 ? (
                <select
                  className="ask-select"
                  value={choice}
                  onChange={(event) =>
                    setValues((current) => ({ ...current, [field.key]: event.target.value }))
                  }
                >
                  <option value="">{field.required ? '请选择（必填）' : '请选择'}</option>
                  {options.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                  {field.allow_custom ? <option value={CUSTOM}>自定义…</option> : null}
                </select>
              ) : null}
              {options.length === 0 || choice === CUSTOM ? (
                <input
                  className="ask-input"
                  value={customs[field.key] ?? ''}
                  placeholder={options.length > 0 ? '自定义输入' : field.default || ''}
                  onChange={(event) =>
                    setCustoms((current) => ({ ...current, [field.key]: event.target.value }))
                  }
                />
              ) : null}
            </div>
          )
        })
      )}
      {missing.length > 0 ? (
        <div className="ask-label" style={{ color: 'var(--danger)' }}>
          还有必填项未回答：{missing.join('、')}
        </div>
      ) : null}
      <div className="approval-actions">
        <button type="button" className="btn primary" onClick={submit}>
          提交回答
        </button>
        <button type="button" className="btn" onClick={() => onAnswer(null)}>
          跳过
        </button>
      </div>
      {approval.timeout ? (
        <div className="approval-note">
          超时 {approval.timeout}s 未应答将按跳过处理（模型不会拿到你的偏好）。
        </div>
      ) : null}
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
