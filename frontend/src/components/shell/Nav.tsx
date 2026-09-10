import type { Note, Project, Session } from '../../api/types'
import type { Route } from '../../router'
import { formatRelative, truncate } from '../../utils/format'

interface NavProps {
  route: Route
  projects: Project[]
  project: Project | null
  sessions: Session[]
  activeSessionId: string | null
  projectNotes: Note[]
  selectedNoteId: string | null
  onSelectProject: (projectId: string) => void
  onNewProject: () => void
  onSelectSession: (sessionId: string) => void
  onNewSession: () => void
  onSelectProjectNote: (noteId: string) => void
  onNewProjectNote: () => void
  onGoHome: () => void
  onGoNotes: () => void
  onGoSkills: () => void
  onGoConfig: () => void
  onGoProjectNotes: () => void
}

export function Nav({
  route,
  projects,
  project,
  sessions,
  activeSessionId,
  projectNotes,
  selectedNoteId,
  onSelectProject,
  onNewProject,
  onSelectSession,
  onNewSession,
  onSelectProjectNote,
  onNewProjectNote,
  onGoHome,
  onGoNotes,
  onGoSkills,
  onGoConfig,
  onGoProjectNotes,
}: NavProps) {
  const inProject = route.kind === 'project'
  const activeProjectId = route.kind === 'project' ? route.projectId : ''

  return (
    <nav className="nav">
      <div className="nav-brand">
        <div className="logo">R</div>
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
              {sessions.length === 0 ? <div className="project-notes-empty">暂无会话</div> : null}
            </div>
          </div>

          <div className="nav-sec project-notes">
            <div className="nav-title">
              项目笔记
              <span>
                <button type="button" className="nav-more" onClick={onGoProjectNotes}>
                  全部
                </button>
                <button type="button" className="nav-new" title="新建项目笔记" onClick={onNewProjectNote}>
                  +
                </button>
              </span>
            </div>
            <div className="project-notes-list">
              {projectNotes.map((note) => (
                <button
                  type="button"
                  key={note.id}
                  className={`project-note-item${note.id === selectedNoteId ? ' active' : ''}`}
                  onClick={() => onSelectProjectNote(note.id)}
                >
                  <div className="project-note-title">{note.title}</div>
                  <div className="project-note-meta">{formatRelative(note.updated_at)}</div>
                </button>
              ))}
              {projectNotes.length === 0 ? (
                <div className="project-notes-empty">暂无关联笔记</div>
              ) : null}
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
            {projects.length === 0 ? <div className="project-notes-empty">暂无项目</div> : null}
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
            <button type="button" className="nav-link" onClick={onGoSkills}>
              <span className="ic">S</span>
              <span className="nav-link-txt">
                <span className="tx">技能</span>
                <div className="sub">/skill 管理</div>
              </span>
            </button>
          </div>
        </div>
      )}

      <div className="nav-ver">Web Console v0.1.0</div>
    </nav>
  )
}
