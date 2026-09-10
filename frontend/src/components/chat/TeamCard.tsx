import type { TeamPayload } from '../../api/types'

interface TeamCardProps {
  payload: TeamPayload
}

/** 团队任务卡：展示 Worker 与角色进度（事件来自 `routivus/agent/team.py` TeamEvent）。 */
export function TeamCard({ payload }: TeamCardProps) {
  const plan = payload.plan
  const tasks = plan?.tasks ?? []
  const activeTask = payload.task
  const statusText =
    payload.kind === 'team_done'
      ? '已完成'
      : payload.kind === 'team_failed'
        ? '失败'
        : payload.kind === 'cancelled'
          ? '已取消'
          : payload.kind === 'team_review'
            ? '待审阅'
            : payload.kind === 'approved'
              ? '已批准'
              : '进行中'

  return (
    <div className="task-card">
      <div className="task-head">
        <span className="task-mode team">团队</span>
        <span className="task-name">{plan?.goal ?? payload.message ?? '团队任务'}</span>
        <span
          className={
            payload.kind === 'team_failed' || payload.kind === 'cancelled' ? 'pill-warn' : 'pill-ok'
          }
        >
          {statusText}
        </span>
      </div>

      {payload.message || payload.failure_category ? (
        <div className="card-desc" style={{ margin: '0 0 7px' }}>
          {payload.message}
          {payload.failure_category ? (
            <span className="mono"> · {payload.failure_category}</span>
          ) : null}
        </div>
      ) : null}

      <div className="worker-row">
        {tasks.map((task) => {
          const running = activeTask?.id === task.id
          return (
            <div
              key={task.id}
              className="worker"
              style={running ? { borderColor: 'var(--border-strong)' } : undefined}
            >
              <div className="w-head">
                <span className="w-name">{task.title}</span>
                <span className="w-tier">{task.owner_role}</span>
              </div>
              <div className="w-desc">{task.description}</div>
            </div>
          )
        })}
        {tasks.length === 0 && payload.role ? (
          <div className="worker">
            <div className="w-head">
              <span className="w-name">{payload.agent_id || payload.role}</span>
              <span className="w-tier">{payload.role}</span>
            </div>
            <div className="w-desc">{payload.message ?? ''}</div>
          </div>
        ) : null}
      </div>
    </div>
  )
}
