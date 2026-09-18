import type { ScopeRequestedData } from '../../api/types'
import { ScopeSelector } from './ScopeSelector'

interface ScopeRequestCardProps {
  payload: ScopeRequestedData
  /** 已提交的授权范围；`null` = 仍在等待用户确认。 */
  submitted: string[] | null
  onSubmit: (taskId: string, scope: string[]) => void
}

/**
 * 消息流末尾的范围确认卡（方案 17 §3.8）。
 *
 * 权限类失败（`needs_scope`）必须由用户**显式选择**写入范围——服务端刻意不从自由
 * 文本里抠路径。这张卡只是把那个入口搬到消息流末尾，解决"要往上翻找团队卡"：
 * 用户说完"继续"就能就地勾选，确认后走现有的 `team_resume` + `scope`。
 *
 * 重连后它不恢复（不在回放白名单里）；团队卡始终是权威展示，再说一次"继续"即可。
 */
export function ScopeRequestCard({ payload, submitted, onSubmit }: ScopeRequestCardProps) {
  return (
    <div className="task-card scope-request">
      <div className="task-head">
        <span className="task-mode team">团队</span>
        <span className="task-name">{payload.goal || '权限不足的任务'}</span>
        <span className="pill-warn">需要确认范围</span>
      </div>
      <div className="card-desc" style={{ margin: '0 0 7px' }}>
        任务 <span className="mono">{payload.task_id}</span> 因无法安全确定写入范围而失败。
        勾选允许 Repairer 修改的范围后继续。
      </div>
      {submitted ? (
        <div className="team-resume-echo">
          已提交授权：<span className="mono">{submitted.join('、')}</span>（write）
        </div>
      ) : (
        <ScopeSelector
          title="允许 Repairer 修改哪些文件？"
          candidates={payload.candidates}
          onSubmit={(scope) => onSubmit(payload.task_id, scope)}
        />
      )}
    </div>
  )
}
