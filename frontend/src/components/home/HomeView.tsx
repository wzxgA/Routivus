import { useEffect, useState } from 'react'
import * as api from '../../api'
import type { ActivityDay, Project } from '../../api/types'
import { formatRelative, formatNumber } from '../../utils/format'
import { describeError } from '../../state/workspace'
import { Empty } from '../common/Empty'
import { Heatmap } from './Heatmap'

interface HomeViewProps {
  projects: Project[]
  loading: boolean
  error: string | null
  onOpenProject: (projectId: string) => void
  onNewProject: () => void
  onRetry: () => void
}

export function HomeView({
  projects,
  loading,
  error,
  onOpenProject,
  onNewProject,
  onRetry,
}: HomeViewProps) {
  const [activity, setActivity] = useState<ActivityDay[]>([])
  const [activityError, setActivityError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    api
      .listActivity(undefined, undefined, controller.signal)
      .then(setActivity)
      .catch((err) => {
        if (err instanceof DOMException && err.name === 'AbortError') return
        setActivityError(describeError(err))
      })
    return () => controller.abort()
  }, [])

  return (
    <section className="view home">
      <div className="home-welcome">
        <div className="page-title">工作台</div>
        <div className="page-sub">选择一个项目开始，或在左侧管理笔记与配置</div>

        {error ? (
          <div className="banner error" style={{ margin: '0 0 12px' }}>
            <span>项目列表加载失败：{error}</span>
            <span className="spacer" />
            <button type="button" className="link-btn" onClick={onRetry}>
              重试
            </button>
          </div>
        ) : null}

        {activityError ? (
          <div className="banner" style={{ margin: '0 0 12px' }}>
            活动统计暂不可用：{activityError}
          </div>
        ) : null}

        <Heatmap days={activity} />

        <div className="card-head">
          <div className="card-name">项目</div>
        </div>
        <div className="proj-grid">
          {projects.map((project) => (
            <button
              type="button"
              key={project.id}
              className="proj-tile"
              onClick={() => onOpenProject(project.id)}
            >
              <div className="pt-top">
                <span className="pt-name">{project.name}</span>
                <span className="pt-act">{formatRelative(project.updated_at)}</span>
              </div>
              <div className="pt-path" title={project.root_path}>
                {project.root_path}
              </div>
              <div className="pt-stats">
                <span>
                  会话 <b>{project.stats.sessions}</b>
                </span>
                <span>
                  笔记 <b>{project.stats.notes}</b>
                </span>
                <span>
                  今日调用 <b>{formatNumber(project.stats.calls_today)}</b>
                </span>
              </div>
            </button>
          ))}
          <button type="button" className="proj-tile new" onClick={onNewProject}>
            + 新建项目
          </button>
        </div>

        {!loading && projects.length === 0 ? (
          <Empty
            title="还没有注册任何项目"
            hint="项目只能注册在服务端允许的工作区根目录下，路径由服务端校验。"
            actionLabel="新建项目"
            onAction={onNewProject}
          />
        ) : null}

        {loading ? <div className="empty">正在加载项目…</div> : null}
      </div>
    </section>
  )
}
