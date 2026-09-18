import { useState, type ReactNode } from 'react'

interface ScopeSelectorProps {
  /** 卡片标题（含任务名等上下文）。 */
  title: ReactNode
  /** 服务端保守提取的候选；**候选不是授权**，勾选才产生授权。 */
  candidates: string[]
  onSubmit: (scope: string[]) => void
  onCancel?: () => void
  submitLabel?: string
}

/**
 * 「允许 Repairer 修改哪些文件」范围选择器（方案 15 §4.6 + 方案 17 §3.8 复用）。
 *
 * 从 `TeamCard` 抽出来，团队卡与消息流末尾的范围确认卡共用同一份交互——差别只在
 * 渲染位置。授权两步：勾选（或自由输入）→ 回显"将授权的范围" → 用户确认。
 *
 * **默认全不勾**：授权这种事的默认值应该是"没有"，不能因为用户说了句"继续"就替他
 * 预先勾好——最小合理猜测也不该自动生效。
 */
export function ScopeSelector({
  title,
  candidates,
  onSubmit,
  onCancel,
  submitLabel = '确认并继续',
}: ScopeSelectorProps) {
  const [picked, setPicked] = useState<Record<string, boolean>>({})
  const [custom, setCustom] = useState('')
  const customPatterns = custom.split(/[\s,，]+/).filter(Boolean)
  const scope = [...candidates.filter((pattern) => picked[pattern]), ...customPatterns]

  return (
    <div className="team-resume">
      <div className="team-resume-title">{title}</div>
      {candidates.length > 0 ? (
        <div className="scope-list">
          {candidates.map((pattern) => (
            <label key={pattern} className="scope-row">
              <input
                type="checkbox"
                checked={Boolean(picked[pattern])}
                onChange={(event) =>
                  setPicked((current) => ({ ...current, [pattern]: event.target.checked }))
                }
              />
              <span className="mono">{pattern}</span>
            </label>
          ))}
        </div>
      ) : (
        <div className="card-desc">
          服务端没能给出候选（该任务没有声明过资源范围），请手动填写要授权的路径。
        </div>
      )}
      <input
        className="scope-input"
        value={custom}
        placeholder="也可以手动补充，空格分隔，例如 routivus/web/** tests/test_web*"
        onChange={(event) => setCustom(event.target.value)}
      />
      <div className="team-resume-echo">
        {scope.length > 0 ? (
          <>
            将授权：<span className="mono">{scope.join('、')}</span>（write）
          </>
        ) : (
          <span className="dim">还没有选择任何范围</span>
        )}
      </div>
      <div className="approval-actions">
        <button
          type="button"
          className="btn primary"
          disabled={scope.length === 0}
          onClick={() => onSubmit(scope)}
        >
          {submitLabel}
        </button>
        {onCancel ? (
          <button type="button" className="btn" onClick={onCancel}>
            返回
          </button>
        ) : null}
      </div>
    </div>
  )
}
