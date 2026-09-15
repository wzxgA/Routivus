import type { RouterDiagnosisView } from '../../api/types'

const LEVEL_TEXT: Record<string, string> = {
  ok: '正常',
  warn: '需注意',
  error: '有问题',
}

/**
 * 结论条：整页最重要的一块。
 *
 * 结论与建议都由后端生成（`server/insights.py`）——门槛常量、原因码、产物来源判定
 * 都在后端，前端复刻一遍必然与实现漂移。这里只按级别上色并把 `hints` 列出来。
 */
export function DiagnosisBanner({ diagnosis }: { diagnosis: RouterDiagnosisView }) {
  return (
    <div className={`ri-diag ri-diag-${diagnosis.level}`}>
      <div className="ri-diag-head">
        <span className="ri-diag-level">{LEVEL_TEXT[diagnosis.level] ?? diagnosis.level}</span>
        <span className="ri-diag-text">{diagnosis.text}</span>
        <span className="ri-diag-code">{diagnosis.code}</span>
      </div>
      {diagnosis.hints.length > 0 ? (
        <ul className="ri-diag-hints">
          {diagnosis.hints.map((hint) => (
            <li key={hint}>{hint}</li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
