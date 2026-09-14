import { useCallback, useEffect, useMemo, useState } from 'react'
import * as api from './api'
import type { Project, Session } from './api/types'
import { ChatView } from './components/chat/ChatView'
import { Modal } from './components/common/Modal'
import { Sky } from './components/common/Sky'
import { ConfigView } from './components/config/ConfigView'
import { FilesView } from './components/files/FilesView'
import { HomeView } from './components/home/HomeView'
import { NotesView } from './components/notes/NotesView'
import { Nav } from './components/shell/Nav'
import { TopBar } from './components/shell/TopBar'
import { SkillsView } from './components/skills/SkillsView'
import {
  CONFIG,
  GLOBAL_NOTES,
  HOME,
  SKILLS,
  navigate,
  projectRoute,
  replaceRoute,
  useRoute,
} from './router'
import { useConfigSnapshot } from './state/config'
import { useProjectWorkspace } from './state/projectWorkspace'
import { describeError } from './state/errors'
import { useWorkspace } from './state/workspaceContext'
import type { ContextPayload, RouterState } from './api/types'
import type { ConnState } from './ws/sessionSocket'

// 窗口不再来自构建期常量：过去这里读 VITE_ROUTIVUS_CONTEXT_WINDOW（全仓库无人设置，
// 恒等于 128000），导致界面显示与后端实际预算各说各话。现在以服务端为准——
// 会话连上后由 `context.updated` / 快照给「本轮模型」的值，未连上时退回配置快照。
const FALLBACK_CONTEXT_WINDOW = 128_000

export function App() {
  const route = useRoute()
  const { projects, loading, error, refresh, theme, toggleTheme } = useWorkspace()
  const configState = useConfigSnapshot()

  const projectId = route.kind === 'project' ? route.projectId : null
  const routeSessionId = route.kind === 'project' ? route.sessionId : null
  const workspace = useProjectWorkspace(projectId, routeSessionId)

  const [renameTarget, setRenameTarget] = useState<Project | null>(null)
  const [removeTarget, setRemoveTarget] = useState<Project | null>(null)
  const [sessionDeleteTarget, setSessionDeleteTarget] = useState<Session | null>(null)
  const [terminalOpen, setTerminalOpen] = useState(false)
  const [filesOpen, setFilesOpen] = useState(false)
  const [hitl, setHitl] = useState<string | null>(null)
  const [router, setRouter] = useState<RouterState | null>(null)
  // 会话当前的窗口 / 输出上限（由 ChatView 上报，含 SmartRouter 换档后的变化）
  const [liveContext, setLiveContext] = useState<ContextPayload | null>(null)
  const [connection, setConnection] = useState<ConnState>('offline')
  const [globalNoteId, setGlobalNoteId] = useState<string | null>(null)
  const [projectNoteSel, setProjectNoteSel] = useState<Record<string, string | null>>({})
  const [newProjectOpen, setNewProjectOpen] = useState(false)

  useEffect(() => {
    if (route.kind !== 'project') {
      setTerminalOpen(false)
      setFilesOpen(false)
      setHitl(null)
      setRouter(null)
      setConnection('offline')
    }
  }, [route.kind])

  // Ctrl+` 折叠 / 展开终端；Ctrl+Shift+E 折叠 / 展开项目文件抽屉（都只在项目态）
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.ctrlKey && event.key === '`') {
        event.preventDefault()
        if (route.kind === 'project') setTerminalOpen((value) => !value)
        return
      }
      if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === 'e') {
        event.preventDefault()
        if (route.kind === 'project' && route.view === 'chat') {
          setFilesOpen((value) => !value)
        }
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [route])

  // 进入项目但 URL 未带 sessionId 时，把自动选中的会话写回地址，保证刷新可恢复。
  useEffect(() => {
    if (route.kind !== 'project') return
    if (route.view !== 'chat' || route.sessionId) return
    if (!workspace.activeSession) return
    replaceRoute(projectRoute(route.projectId, 'chat', workspace.activeSession.id))
  }, [route, workspace.activeSession])

  const handleSelectProject = useCallback((id: string) => {
    navigate(projectRoute(id))
  }, [])

  const handleNewSession = useCallback(async () => {
    if (!projectId) return
    const session = await workspace.createSession()
    if (session) navigate(projectRoute(projectId, 'chat', session.id))
  }, [projectId, workspace])

  const handleSelectSession = useCallback(
    (sessionId: string) => {
      if (!projectId) return
      navigate(projectRoute(projectId, 'chat', sessionId))
    },
    [projectId],
  )

  const handleNewProjectNote = useCallback(async () => {
    if (!projectId) return
    try {
      const created = await api.createProjectNote(projectId, {
        title: '未命名笔记',
        body_markdown: '',
        tags: [],
      })
      setProjectNoteSel((current) => ({ ...current, [projectId]: created.id }))
      navigate(projectRoute(projectId, 'notes'))
      await workspace.refreshNotes()
    } catch {
      /* 失败时停留在原视图，NotesView 会展示列表错误 */
    }
  }, [projectId, workspace])

  const handleSelectProjectNote = useCallback(
    (noteId: string) => {
      if (!projectId) return
      setProjectNoteSel((current) => ({ ...current, [projectId]: noteId }))
      navigate(projectRoute(projectId, 'notes'))
    },
    [projectId],
  )

  const handleNotesMutated = useCallback(() => {
    void workspace.refreshNotes()
    void refresh()
  }, [workspace, refresh])

  const selectedNoteId =
    route.kind === 'project' ? (projectNoteSel[projectId ?? ''] ?? null) : globalNoteId

  const handleSelectNote = useCallback(
    (noteId: string | null) => {
      if (route.kind === 'project' && projectId) {
        setProjectNoteSel((current) => ({ ...current, [projectId]: noteId }))
      } else {
        setGlobalNoteId(noteId)
      }
    },
    [route.kind, projectId],
  )

  const activeProject = workspace.project
  const liveSession: Session | null = workspace.activeSession

  // 顶栏切换运行模型：写全局 active provider/model。会话侧会复用已建立的 Agent，
  // 所以对既有会话要新建会话才生效（顶栏弹层里已注明）。
  const handleSwitchModel = useCallback(
    async (provider: string, model: string) => {
      await api.setActiveProvider(provider, model)
      await configState.reload()
    },
    [configState],
  )

  // 窗口的两个来源：会话实时的（含 SmartRouter 换档后的变化）> 配置快照（还没连上
  // 会话时）> 兜底。之前这里是构建期常量，与后端算的不是同一个数。
  const contextWindow =
    liveContext?.window ?? configState.config?.context_window ?? FALLBACK_CONTEXT_WINDOW

  const view = useMemo(() => {
    switch (route.kind) {
      case 'home':
        return (
          <HomeView
            projects={projects}
            loading={loading}
            error={error}
            onOpenProject={handleSelectProject}
            onNewProject={() => setNewProjectOpen(true)}
            onRenameProject={setRenameTarget}
            onRemoveProject={setRemoveTarget}
            onRetry={() => void refresh()}
          />
        )
      case 'notes':
        return (
          <NotesView
            scope="global"
            projectId={null}
            projectName={null}
            selectedNoteId={globalNoteId}
            onSelectNote={handleSelectNote}
            onMutated={handleNotesMutated}
          />
        )
      case 'skills':
        return <SkillsView projects={projects} />
      case 'config':
        return (
          <ConfigView
            config={configState.config}
            loading={configState.loading}
            error={configState.error}
            onReload={configState.reload}
            router={router}
          />
        )
      case 'project': {
        if (workspace.error) {
          return (
            <section className="view view-page">
              <div className="page-title">{activeProject?.name ?? '项目'}</div>
              <div className="banner error" style={{ margin: '10px 0' }}>
                <span>{workspace.error}</span>
                <span className="spacer" />
                <button type="button" className="link-btn" onClick={() => void workspace.refresh()}>
                  重试
                </button>
              </div>
              <button type="button" className="btn" onClick={() => navigate(HOME)}>
                返回首页
              </button>
            </section>
          )
        }
        if (route.view === 'notes') {
          return (
            <NotesView
              scope="project"
              projectId={route.projectId}
              projectName={activeProject?.name ?? null}
              selectedNoteId={projectNoteSel[route.projectId] ?? null}
              onSelectNote={(noteId) =>
                setProjectNoteSel((current) => ({ ...current, [route.projectId]: noteId }))
              }
              onMutated={handleNotesMutated}
            />
          )
        }
        if (route.view === 'files') {
          return (
            <FilesView
              projectId={route.projectId}
              projectName={activeProject?.name ?? 'project'}
              projectPath={activeProject?.root_path ?? null}
            />
          )
        }
        return (
          <ChatView
            projectId={route.projectId}
            projectName={activeProject?.name ?? 'project'}
            projectPath={activeProject?.root_path ?? null}
            session={liveSession}
            projectNotesCount={workspace.projectNotes.length}
            terminalOpen={terminalOpen}
            filesOpen={filesOpen}
            contextWindow={contextWindow}
            onToggleTerminal={() => setTerminalOpen((value) => !value)}
            onToggleFiles={() => setFilesOpen((value) => !value)}
            onOpenFilesPage={() => navigate(projectRoute(route.projectId, 'files'))}
            onSessionUpdate={workspace.applySessionUpdate}
            onHitlChange={setHitl}
            onRouterChange={setRouter}
            onContextChange={setLiveContext}
            onConnectionChange={setConnection}
          />
        )
      }
      default:
        return null
    }
  }, [
    route,
    projects,
    loading,
    error,
    refresh,
    activeProject,
    liveSession,
    workspace,
    terminalOpen,
    filesOpen,
    projectNoteSel,
    globalNoteId,
    handleSelectNote,
    handleSelectProject,
    handleNotesMutated,
    configState,
    contextWindow,
    router,
  ])

  const inProject = route.kind === 'project'

  return (
    <div className={`app${inProject ? ' in-project' : ''}`}>
      <Sky />
      <Nav
        route={route}
        projects={projects}
        project={activeProject}
        sessions={workspace.sessions}
        activeSessionId={workspace.activeSession?.id ?? null}
        projectNotes={workspace.projectNotes}
        selectedNoteId={selectedNoteId}
        onSelectProject={handleSelectProject}
        onNewProject={() => setNewProjectOpen(true)}
        onSelectSession={handleSelectSession}
        onNewSession={() => void handleNewSession()}
        onRequestDeleteSession={setSessionDeleteTarget}
        onSelectProjectNote={handleSelectProjectNote}
        onNewProjectNote={() => void handleNewProjectNote()}
        onGoHome={() => navigate(HOME)}
        onGoNotes={() => navigate(GLOBAL_NOTES)}
        onGoSkills={() => navigate(SKILLS)}
        onGoConfig={() => navigate(CONFIG)}
        onGoProjectNotes={() => {
          if (projectId) navigate(projectRoute(projectId, 'notes'))
        }}
      />
      <div className="right">
        <TopBar
          route={route}
          project={activeProject}
          liveSession={liveSession}
          projectNotesCount={workspace.projectNotes.length}
          connection={inProject ? connection : 'offline'}
          theme={theme}
          contextWindow={contextWindow}
          hitl={hitl}
          router={router}
          terminalOpen={terminalOpen}
          filesOpen={filesOpen}
          config={configState.config}
          onSwitchModel={handleSwitchModel}
          onToggleTheme={toggleTheme}
          onToggleTerminal={() => setTerminalOpen((value) => !value)}
          onToggleFiles={() => setFilesOpen((value) => !value)}
          onGoHome={() => navigate(HOME)}
          onGoNotes={() => navigate(GLOBAL_NOTES)}
          onGoConfig={() => navigate(CONFIG)}
          onGoChat={() => {
            if (projectId) navigate(projectRoute(projectId, 'chat', workspace.activeSession?.id ?? null))
          }}
          onGoProjectNotes={() => {
            if (projectId) navigate(projectRoute(projectId, 'notes'))
          }}
          onGoProjectFiles={() => {
            if (projectId) navigate(projectRoute(projectId, 'files'))
          }}
        />
        <div className="views">{view}</div>
        {inProject && route.kind === 'project' && route.view === 'chat' ? (
          <div className="foot">
            <span>
              <kbd>Enter</kbd>发送
            </span>
            <span>
              <kbd>↑↓</kbd>历史
            </span>
            <span>
              <kbd>Tab</kbd>补全
            </span>
            <span>
              <kbd>Ctrl</kbd>
              <kbd>`</kbd>终端
            </span>
            <span>
              <kbd>Esc</kbd>关闭弹层
            </span>
          </div>
        ) : null}
      </div>

      {newProjectOpen ? (
        <NewProjectModal
          onClose={() => setNewProjectOpen(false)}
          onCreated={(id) => {
            setNewProjectOpen(false)
            void refresh()
            navigate(projectRoute(id))
          }}
        />
      ) : null}

      {renameTarget ? (
        <RenameProjectModal
          project={renameTarget}
          onClose={() => setRenameTarget(null)}
          onDone={async () => {
            setRenameTarget(null)
            await refresh()
          }}
        />
      ) : null}

      {removeTarget ? (
        <RemoveProjectModal
          project={removeTarget}
          onClose={() => setRemoveTarget(null)}
          onDone={async () => {
            const removedId = removeTarget.id
            setRemoveTarget(null)
            await refresh()
            // 若当前正停留在被移除的项目里，退回首页，避免停留在悬空路由
            if (projectId === removedId) navigate(HOME)
          }}
        />
      ) : null}

      {sessionDeleteTarget ? (
        <RemoveSessionModal
          session={sessionDeleteTarget}
          onClose={() => setSessionDeleteTarget(null)}
          onConfirm={async () => {
            const target = sessionDeleteTarget
            await workspace.removeSession(target.id)
            setSessionDeleteTarget(null)
            // 删的正是地址栏里那个会话：换到列表里的下一个，别停在悬空路由上。
            // 一个都不剩就置空 sessionId，由 App 的兜底（或 hook 的自动建首个会话）接管。
            if (routeSessionId === target.id && projectId) {
              const next = workspace.sessions.find((item) => item.id !== target.id)
              navigate(projectRoute(projectId, 'chat', next?.id ?? null))
            }
          }}
        />
      ) : null}
    </div>
  )
}

function NewProjectModal({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (projectId: string) => void
}) {
  const [name, setName] = useState('')
  const [rootPath, setRootPath] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [picking, setPicking] = useState(false)

  // 桌面端：走系统目录选择器。用户亲手选中的目录即显式授权，先加入运行时
  // 白名单再注册，避免让用户手打路径（也避免把白名单默认放宽到 home）。
  const pickDirectory = async () => {
    const bridge = window.routivus
    if (!bridge) return
    setPicking(true)
    setError(null)
    try {
      const picked = await bridge.pickDirectory()
      if (!picked) return
      await api.grantWorkspaceRoot(picked)
      setRootPath(picked)
      if (!name.trim()) {
        const segments = picked.split(/[\\/]/).filter(Boolean)
        setName(segments[segments.length - 1] ?? '')
      }
    } catch (err) {
      setError(describeError(err))
    } finally {
      setPicking(false)
    }
  }

  const submit = async () => {
    if (!name.trim() || !rootPath.trim()) {
      setError('名称与路径都不能为空')
      return
    }
    setSaving(true)
    try {
      const project = await api.createProject({ name: name.trim(), root_path: rootPath.trim() })
      onCreated(project.id)
    } catch (err) {
      setError(describeError(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title="新建项目"
      description="路径会在服务端解析为绝对路径，并校验是否位于允许的工作区根目录内。"
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn primary" disabled={saving} onClick={() => void submit()}>
            {saving ? '创建中…' : '创建'}
          </button>
        </>
      }
    >
      {error ? (
        <div className="banner error" style={{ margin: '0 0 10px' }}>
          {error}
        </div>
      ) : null}
      <div className="field">
        <label htmlFor="project-name">项目名称</label>
        <input
          id="project-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="例如：Nimbus"
        />
      </div>
      <div className="field">
        <label htmlFor="project-path">项目根目录</label>
        <div style={{ display: 'flex', gap: 6 }}>
          <input
            id="project-path"
            value={rootPath}
            onChange={(event) => setRootPath(event.target.value)}
            placeholder="D:\DevProject\Nimbus"
          />
          {window.routivus ? (
            <button
              type="button"
              className="btn"
              style={{ flex: '0 0 auto' }}
              disabled={picking}
              onClick={() => void pickDirectory()}
            >
              {picking ? '选择中…' : '浏览…'}
            </button>
          ) : null}
        </div>
        <div className="ed-meta" style={{ marginTop: 4 }}>
          {window.routivus
            ? '通过系统目录选择器选中即完成授权，无需手输路径。'
            : '路径需位于服务端允许的工作区根目录内（ROUTIVUS_WORKSPACE_ROOTS）。'}
        </div>
      </div>
    </Modal>
  )
}

/** 重命名项目：只改显示名，项目根目录不变。 */
function RenameProjectModal({
  project,
  onClose,
  onDone,
}: {
  project: Project
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const [name, setName] = useState(project.name)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    if (!name.trim()) {
      setError('项目名称不能为空')
      return
    }
    setSaving(true)
    setError(null)
    try {
      await api.updateProject(project.id, { name: name.trim() })
      await onDone()
    } catch (err) {
      setError(describeError(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={`重命名项目：${project.name}`}
      description={`根目录保持不变：${project.root_path}`}
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={saving || !name.trim() || name.trim() === project.name}
            onClick={() => void submit()}
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {error ? (
        <div className="banner error" style={{ margin: '0 0 10px' }}>
          {error}
        </div>
      ) : null}
      <div className="field">
        <label htmlFor="rename-project">项目名称</label>
        <input
          id="rename-project"
          value={name}
          autoFocus
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void submit()
          }}
        />
      </div>
    </Modal>
  )
}

/** 移除项目：只摘注册表，不删磁盘文件与会话 / 笔记数据。 */
function RemoveProjectModal({
  project,
  onClose,
  onDone,
}: {
  project: Project
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    setSaving(true)
    setError(null)
    try {
      await api.deleteProject(project.id)
      await onDone()
    } catch (err) {
      setError(describeError(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={`移除项目：${project.name}`}
      description="只从项目列表移除，不会删除磁盘上的任何文件。"
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn danger" disabled={saving} onClick={() => void submit()}>
            {saving ? '移除中…' : '移除'}
          </button>
        </>
      }
    >
      {error ? (
        <div className="banner error" style={{ margin: '0 0 10px' }}>
          {error}
        </div>
      ) : null}
      <div className="ed-meta">
        根目录：<code>{project.root_path}</code>
      </div>
      <div className="ed-meta" style={{ marginTop: 6 }}>
        该项目的会话、消息与笔记仍保留在本地数据库中；重新添加同一路径即可恢复可见。
        若该项目仍有运行中的会话，服务端会拒绝移除（先停止会话）。
      </div>
    </Modal>
  )
}

/**
 * 删除会话的确认框（左侧会话列表右键触发）。
 *
 * 与「移除项目」的区别：那是摘注册、数据都留着；这里是**真删**——会话连同它的
 * 消息与事件一起从 SQLite 里消失（外键级联），所以文案必须说清不可恢复。
 */
function RemoveSessionModal({
  session,
  onClose,
  onConfirm,
}: {
  session: Session
  onClose: () => void
  onConfirm: () => Promise<void>
}) {
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  async function submit() {
    setError(null)
    setSaving(true)
    try {
      await onConfirm()
    } catch (err) {
      setError(describeError(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={`删除会话：${session.title}`}
      description="会话连同它的消息与事件一起从本地数据库删除，此操作不可撤销。"
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn danger"
            disabled={saving}
            onClick={() => void submit()}
          >
            {saving ? '删除中…' : '删除'}
          </button>
        </>
      }
    >
      {error ? (
        <div className="banner error" style={{ margin: '0 0 10px' }}>
          {error}
        </div>
      ) : null}
      <div className="ed-meta">
        状态：{session.status} · 累计 {session.total_tokens} tokens
      </div>
      <div className="ed-meta" style={{ marginTop: 6 }}>
        正在运行的会话不能删除（服务端会拒绝），请先停止或取消当前任务。
      </div>
    </Modal>
  )
}
