/**
 * 团队任务的状态文案与任务标记（方案 16）。
 *
 * 抽出来的理由：同一个状态要在两处显示——消息流的 TeamCard 是"事件现场"（随对话
 * 滚动），侧栏 Team 页签是"常驻总览"。各写一份映射迟早文案漂移，出现同一个任务
 * 在卡上叫"失败"、在页签里叫别的的情况。
 */

/** 团队整体状态（`TeamPayload.kind` → 文案）。 */
export function teamStatusText(kind: string): string {
  switch (kind) {
    case 'team_done':
      return '已完成'
    case 'team_failed':
      return '失败'
    case 'cancelled':
      return '已取消'
    case 'team_review':
      return '待审阅'
    case 'approved':
      return '已批准'
    default:
      return '进行中'
  }
}

/** 状态色调：失败 / 取消走告警色，其余走正常色（对应 `.pill-warn` / `.pill-ok`）。 */
export function teamStatusTone(kind: string): 'ok' | 'warn' {
  return kind === 'team_failed' || kind === 'cancelled' ? 'warn' : 'ok'
}

export interface TaskMark {
  cls: 'mk-done' | 'mk-run' | 'mk-fail' | 'mk-todo'
  glyph: string
}

/**
 * 任务级标记，复用 Plan 页签那套 `mk-*` 体系（`app.css`）。
 *
 * `TeamTask.status` 是可选的（未开始的任务可能没有这个字段），所以默认值必须是
 * "待办"而不是"完成"。
 */
export function teamTaskMark(status: string | undefined): TaskMark {
  switch (status) {
    case 'done':
      return { cls: 'mk-done', glyph: '✓' }
    case 'running':
      return { cls: 'mk-run', glyph: '●' }
    case 'failed':
      return { cls: 'mk-fail', glyph: '✕' }
    default:
      return { cls: 'mk-todo', glyph: '○' }
  }
}
