import type { Project, Session } from '../../api/types'
import type { Route } from '../../router'
import { formatRelative, truncate } from '../../utils/format'
import { ContextMenu } from '../common/ContextMenu'
import { useContextMenu } from '../common/useContextMenu'

interface NavProps {
  route: Route
  projects: Project[]
  project: Project | null
  sessions: Session[]
  activeSessionId: string | null
  onSelectProject: (projectId: string) => void
  onNewProject: () => void
  onSelectSession: (sessionId: string) => void
  onNewSession: () => void
  /** 右键「重命名会话」：由上层弹输入框后执行（只改显示名）。 */
  onRequestRenameSession: (session: Session) => void
  /** 右键「删除会话」：由上层弹确认框后真正执行（删除是不可逆的）。 */
  onRequestDeleteSession: (session: Session) => void
  onGoHome: () => void
  onGoNotes: () => void
  onGoSkills: () => void
  onGoConfig: () => void
}

export function Nav({
  route,
  projects,
  project,
  sessions,
  activeSessionId,
  onSelectProject,
  onNewProject,
  onSelectSession,
  onNewSession,
  onRequestRenameSession,
  onRequestDeleteSession,
  onGoHome,
  onGoNotes,
  onGoSkills,
  onGoConfig,
}: NavProps) {
  const inProject = route.kind === 'project'
  const activeProjectId = route.kind === 'project' ? route.projectId : ''
  const sessionMenu = useContextMenu<Session>()
  // 存成局部常量：闭包里才能保住类型收窄（属性访问的收窄进不了回调）
  const openSessionMenu = sessionMenu.menu

  return (
    <nav className="nav">
      <div className="nav-brand">
        {/* 标记本身是 CSS 背景图：日/夜两版按 data-theme 换（见 app.css 的 .logo） */}
        <span className="logo" role="img" aria-label="Routivus" />
        <div>
          <div className="brand-name">Routivus</div>
          <div className="brand-sub">Web Console</div>
        </div>
      </div>

      {inProject ? (
        <div className="nav-project">
          <button type="button" className="nav-back" onClick={onGoHome}>
            ← 所有项目
          </button>

          {project ? (
            <div className="proj-card">
              <div className="proj-name">{project.name}</div>
              <div className="proj-path" title={project.root_path}>
                {project.root_path}
              </div>
              <div className="proj-meta">
                {project.branch ? <span className="tag-branch">{project.branch}</span> : null}
                <span className="proj-status">● {project.status}</span>
              </div>
            </div>
          ) : null}

          <div className="nav-sec grow">
            <div className="nav-title">
              会话
              <button type="button" className="nav-new" title="新建会话" onClick={onNewSession}>
                +
              </button>
            </div>
            <div className="sess-list">
              {sessions.map((session) => {
                const active = session.id === activeSessionId
                return (
                  <button
                    type="button"
                    key={session.id}
                    className={`sess-item${active ? ' active' : ''}`}
                    onClick={() => onSelectSession(session.id)}
                    onContextMenu={(event) => sessionMenu.open(session, event)}
                  >
                    <div className="sess-top">
                      <span className="sess-name">{session.title}</span>
                      {active ? <span className="tag-sel">当前</span> : null}
                    </div>
                    <div className="sess-meta">
                      {session.total_tokens} tk · {formatRelative(session.updated_at)}
                    </div>
                  </button>
                )
              })}
              {sessions.length === 0 ? <div className="nav-empty">暂无会话</div> : null}
            </div>
          </div>
        </div>
      ) : (
        <div className="nav-global">
          <div className="nav-sec">
            <div className="nav-title">
              项目
              <button type="button" className="nav-new" title="新建项目" onClick={onNewProject}>
                +
              </button>
            </div>
            {projects.map((item) => (
              <button
                type="button"
                key={item.id}
                className="proj-entry"
                onClick={() => onSelectProject(item.id)}
                title={item.root_path}
              >
                <div className="pe-top">
                  <span className="pe-name">{item.name}</span>
                  {item.id === activeProjectId ? <span className="tag-sel">当前</span> : null}
                </div>
                <div className="pe-meta">
                  {truncate(item.root_path, 30)} · {formatRelative(item.updated_at)}
                </div>
              </button>
            ))}
            {projects.length === 0 ? <div className="nav-empty">暂无项目</div> : null}
          </div>

          <div className="nav-sec">
            <div className="nav-title">工作区</div>
            <button
              type="button"
              className={`nav-link${route.kind === 'notes' ? ' active' : ''}`}
              onClick={onGoNotes}
            >
              <span className="ic">N</span>
              <span className="nav-link-txt">
                <span className="tx">全局笔记</span>
                <div className="sub">全部笔记及项目归属</div>
              </span>
            </button>
            <button
              type="button"
              className={`nav-link${route.kind === 'config' ? ' active' : ''}`}
              onClick={onGoConfig}
            >
              <span className="ic">C</span>
              <span className="nav-link-txt">
                <span className="tx">配置</span>
                <div className="sub">Provider · 四档 · HITL</div>
              </span>
            </button>
            <button
              type="button"
              className={`nav-link${route.kind === 'skills' ? ' active' : ''}`}
              onClick={onGoSkills}
            >
              <span className="ic">S</span>
              <span className="nav-link-txt">
                <span className="tx">技能</span>
                <div className="sub">任务规范管理</div>
              </span>
            </button>
          </div>
        </div>
      )}

      {openSessionMenu ? (
        <ContextMenu
          x={openSessionMenu.x}
          y={openSessionMenu.y}
          onClose={sessionMenu.close}
          items={[
            {
              key: 'rename-session',
              label: '重命名会话',
              onSelect: () => onRequestRenameSession(openSessionMenu.target),
            },
            {
              key: 'delete-session',
              label: '删除会话',
              danger: true,
              onSelect: () => onRequestDeleteSession(openSessionMenu.target),
            },
          ]}
        />
      ) : null}

      <div className="nav-ver">Web Console v0.1.0</div>
    </nav>
  )
}
