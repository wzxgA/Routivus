import { useState } from 'react'
import type { PlanReviewRequest } from '../../api/types'

interface PlanReviewCardProps {
  review: PlanReviewRequest
  onDecide: (action: 'execute' | 'cancel' | 'replan', feedback?: string) => void
}

/**
 * 计划 / 团队审阅卡。
 *
 * 服务端 `/plan`、`/team` 在生成计划后阻塞等待决策（`PlanReviewBridge`），
 * 批准之前不会执行任何工具。交互与原型一致：批准执行 / 重新规划 / 取消。
 */
export function PlanReviewCard({ review, onDecide }: PlanReviewCardProps) {
  const [replanning, setReplanning] = useState(false)
  const [feedback, setFeedback] = useState('')
  const tasks = review.plan?.tasks ?? []

  return (
    <div className="approval">
      <div className="approval-title">
        <span className="approval-risk">{review.mode === 'team' ? '团队审阅' : '计划审阅'}</span>
        {review.plan?.goal || '计划已生成，等待确认'}
      </div>

      {tasks.length ? (
        <ul className="steps">
          {tasks.map((task) => (
            <li key={task.id}>
              <span className="mk mk-todo">○</span>
              <span>
                {task.id} {task.title}
                {task.owner_role ? ` · ${task.owner_role}` : ''}
                {task.deps.length ? `（依赖 ${task.deps.join(', ')}）` : ''}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="card-desc">计划内容为空。</div>
      )}

      {replanning ? (
        <div className="ask-field">
          <div className="ask-label">重新规划要求（会作为反馈重新拆解）</div>
          <textarea
            className="ask-textarea"
            value={feedback}
            placeholder="例如：把数据库迁移拆成独立子任务，并先补回归测试"
            onChange={(event) => setFeedback(event.target.value)}
          />
        </div>
      ) : null}

      <div className="approval-actions">
        {replanning ? (
          <>
            <button
              type="button"
              className="btn primary"
              disabled={!feedback.trim()}
              onClick={() => onDecide('replan', feedback.trim())}
            >
              提交并重新规划
            </button>
            <button type="button" className="btn" onClick={() => setReplanning(false)}>
              返回
            </button>
          </>
        ) : (
          <>
            <button type="button" className="btn primary" onClick={() => onDecide('execute')}>
              批准并执行
            </button>
            <button type="button" className="btn" onClick={() => setReplanning(true)}>
              重新规划
            </button>
            <button type="button" className="btn danger" onClick={() => onDecide('cancel')}>
              取消计划
            </button>
          </>
        )}
      </div>

      <div className="approval-note">
        {review.timeout ? `超时 ${review.timeout}s 未应答将按取消处理；` : ''}
        批准前不会执行任何工具。
      </div>
    </div>
  )
}
