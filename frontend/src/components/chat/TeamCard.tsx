import { useState } from 'react'
import type { TeamPayload } from '../../api/types'

interface TeamCardProps {
  payload: TeamPayload
  /**
   * 点了「继续」（方案 15 §4.4）。带 `scope` = 补一个写入范围救那个任务；
   * 不带 = 续跑整轮（done 跳过、其余重跑）。
   */
  onResume?: (options: { taskId?: string; scope?: string[] }) => void
  /** 会话里刚说了句"继续"：把按钮亮一下（方案 15 §4.9，仍要用户点，不自动执行）。 */
  highlight?: boolean
}

/** 团队任务卡：展示 Worker 与角色进度（事件来自 `routivus/agent/team.py` TeamEvent）。 */
export function TeamCard({ payload, onResume, highlight = false }: TeamCardProps) {
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

  // 「补范围救任务」（方案 15 §4.6）：只有"无法安全确定写入范围"而失败的任务会被
  // 服务端标记 needs_scope。候选来自服务端保守提取（任务已声明的 claims + 同伴的
  // write 范围），**勾选才产生授权**——候选本身不是授权。
  const scopeTask = tasks.find((task) => task.needs_scope && task.status === 'failed') ?? null
  const candidates = scopeTask?.scope_candidates ?? []
  const [picked, setPicked] = useState<Record<string, boolean>>({})
  const [custom, setCustom] = useState('')
  const [picking, setPicking] = useState(false)
  const customPatterns = custom.split(/[\s,，]+/).filter(Boolean)
  const scope = [...candidates.filter((pattern) => picked[pattern]), ...customPatterns]
  const doneCount = tasks.filter((task) => task.status === 'done').length

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

      {scopeTask && picking ? (
        <div className="team-resume">
          <div className="team-resume-title">
            允许 Repairer 修改哪些文件？<span className="dim">（{scopeTask.title}）</span>
          </div>
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
              onClick={() => onResume?.({ taskId: scopeTask.id, scope })}
            >
              确认并继续
            </button>
            <button type="button" className="btn" onClick={() => setPicking(false)}>
              返回
            </button>
          </div>
        </div>
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
