import { useState } from 'react'
import type { TeamPayload } from '../../api/types'
import { teamStatusText, teamStatusTone } from '../../utils/teamStatus'
import { ScopeSelector } from './ScopeSelector'

interface TeamCardProps {
  payload: TeamPayload
  /**
   * 点了「继续」（方案 15 §4.4）。带 `scope` = 补一个写入范围救那个任务；
   * 不带 = 续跑整轮（done 跳过、其余重跑）。
   */
  onResume?: (options: { taskId?: string; scope?: string[] }) => void
  /** 会话里刚说了句"继续"（方案 17 §3.6）：把按钮亮一下作回声，仍要用户点。 */
  highlight?: boolean
}

/** 团队任务卡：展示 Worker 与角色进度（事件来自 `routivus/agent/team.py` TeamEvent）。 */
export function TeamCard({ payload, onResume, highlight = false }: TeamCardProps) {
  const plan = payload.plan
  const tasks = plan?.tasks ?? []
  const activeTask = payload.task
  // 文案与色调走共享函数：侧栏 Team 页签显示的是同一份（方案 16 §6）
  const statusText = teamStatusText(payload.kind)
  const statusTone = teamStatusTone(payload.kind)

  // 「补范围救任务」（方案 15 §4.6）：只有"无法安全确定写入范围"而失败的任务会被
  // 服务端标记 needs_scope。候选来自服务端保守提取（任务已声明的 claims + 同伴的
  // write 范围），**勾选才产生授权**——候选本身不是授权。
  const scopeTask = tasks.find((task) => task.needs_scope && task.status === 'failed') ?? null
  const [picking, setPicking] = useState(false)
  const doneCount = tasks.filter((task) => task.status === 'done').length

  return (
    <div className="task-card">
      <div className="task-head">
        <span className="task-mode team">团队</span>
        <span className="task-name">{plan?.goal ?? payload.message ?? '团队任务'}</span>
        <span className={statusTone === 'warn' ? 'pill-warn' : 'pill-ok'}>
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

      {scopeTask && picking ? (
        // 范围选择器与消息流末尾的确认卡共用同一份实现（方案 17 §3.8）。
        <ScopeSelector
          title={
            <>
              允许 Repairer 修改哪些文件？
              <span className="dim">（{scopeTask.title}）</span>
            </>
          }
          candidates={scopeTask.scope_candidates ?? []}
          onCancel={() => setPicking(false)}
          onSubmit={(scope) => onResume?.({ taskId: scopeTask.id, scope })}
        />
      ) : payload.resumable && onResume ? (
        <div className="team-resume-actions">
          {scopeTask ? (
            // 有"补个范围就能救"的任务时**只给范围入口**：整轮续跑会把那个任务原样重跑
            // 一遍、大概率用同样的原因再失败一次，白烧预算（方案 15 §8 风险 5 的同源考虑）。
            <button type="button" className="btn primary" onClick={() => setPicking(true)}>
              选择修改范围并继续
            </button>
          ) : (
            <button
              type="button"
              className={highlight ? 'btn resume-hint' : 'btn'}
              onClick={() => onResume({})}
            >
              继续{doneCount > 0 ? `（跳过 ${doneCount} 个已完成）` : ''}
            </button>
          )}
        </div>
      ) : null}
    </div>
  )
}
