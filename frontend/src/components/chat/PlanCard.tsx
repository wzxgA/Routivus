import { useState } from 'react'
import type { PlanPayload, PlanTask } from '../../api/types'

const MARK: Record<string, { glyph: string; cls: string }> = {
  done: { glyph: '✓', cls: 'mk-done' },
  running: { glyph: '●', cls: 'mk-run' },
  failed: { glyph: '✕', cls: 'mk-fail' },
  pending: { glyph: '○', cls: 'mk-todo' },
}

interface PlanCardProps {
  payload: PlanPayload
}

/**
 * 计划任务卡：列表 / 流程图双视图。
 *
 * 事件契约来自 `routivus/agent/plan.py` 的 PlanEvent；后端经 `plan.updated` 转推。
 * 批准 / 取消 / 重新规划由计划审阅卡（`PlanReviewCard`）完成，卡内不做隐式执行。
 */
export function PlanCard({ payload }: PlanCardProps) {
  const [view, setView] = useState<'list' | 'flow'>('list')
  const plan = payload.plan
  const tasks = plan?.tasks ?? []

  const statusText =
    payload.kind === 'plan_done'
      ? '已完成'
      : payload.kind === 'plan_failed'
        ? '失败'
        : payload.kind === 'cancelled'
          ? '已取消'
          : payload.kind === 'review'
            ? '待审阅'
            : payload.kind === 'approved'
              ? '已批准'
              : '进行中'
  const warn = payload.kind === 'plan_failed' || payload.kind === 'cancelled'

  const nodeClass = (task: PlanTask) =>
    task.status === 'done' ? 'done' : task.status === 'running' ? 'run' : 'todo'

  const renderNode = (task: PlanTask) => (
    <div key={task.id} className={`dag-node ${nodeClass(task)}`} title={task.description}>
      <div className="dag-top">
        <span className="dag-idx">{task.id}</span>
        <span className="dag-mark">{MARK[task.status]?.glyph ?? '○'}</span>
      </div>
      <div className="dag-title">{task.title}</div>
      {task.deps.length ? <div className="dag-branch">依赖 {task.deps.join(', ')}</div> : null}
    </div>
  )

  const batches: string[][] = plan?.batches?.length
    ? plan.batches
    : tasks.map((task) => [task.id])

  return (
    <div className="task-card">
      <div className="task-head">
        <span className="task-mode plan">计划</span>
        <span className="task-name">{plan?.goal ?? payload.message ?? '计划'}</span>
        <span className={warn ? 'pill-warn' : 'pill-ok'}>{statusText}</span>
        <div className="task-switch">
          <button
            type="button"
            className={`switch-btn${view === 'list' ? ' active' : ''}`}
            onClick={() => setView('list')}
          >
            列表
          </button>
          <button
            type="button"
            className={`switch-btn${view === 'flow' ? ' active' : ''}`}
            onClick={() => setView('flow')}
          >
            流程图
          </button>
        </div>
      </div>

      {payload.message ? (
        <div className="card-desc" style={{ margin: '0 0 7px' }}>
          {payload.message}
        </div>
      ) : null}

      {view === 'list' ? (
        <ul className="steps">
          {tasks.map((task) => (
            <li key={task.id}>
              <span className={`mk ${MARK[task.status]?.cls ?? 'mk-todo'}`}>
                {MARK[task.status]?.glyph ?? '○'}
              </span>
              <span className={task.status === 'done' ? 'step-done' : task.status === 'running' ? 'step-run' : ''}>
                {task.id} {task.title}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="dag">
          {batches.map((batch, index) => (
            <div key={`batch-${index}`}>
              {batch.length > 1 ? (
                <div className="dag-fork">
                  <span className="dag-varrow" />
                  <span className="dag-varrow" />
                </div>
              ) : index > 0 ? (
                <div className="dag-varrow" />
              ) : null}
              {batch.length > 1 ? (
                <div className="dag-subrow">
                  {batch
                    .map((id) => tasks.find((task) => task.id === id))
                    .filter((task): task is PlanTask => Boolean(task))
                    .map(renderNode)}
                </div>
              ) : (
                batch
                  .map((id) => tasks.find((task) => task.id === id))
                  .filter((task): task is PlanTask => Boolean(task))
                  .map(renderNode)
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
