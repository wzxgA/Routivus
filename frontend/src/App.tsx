import { useCallback, useEffect, useMemo, useState } from 'react'
import * as api from './api'
import type { Session } from './api/types'
import { ChatView } from './components/chat/ChatView'
import { Modal } from './components/common/Modal'
import { Sky } from './components/common/Sky'
import { ConfigView } from './components/config/ConfigView'
import { HomeView } from './components/home/HomeView'
import { NotesView } from './components/notes/NotesView'
import { Nav } from './components/shell/Nav'
import { TopBar } from './components/shell/TopBar'
import {
  CONFIG,
  GLOBAL_NOTES,
  HOME,
  navigate,
  projectRoute,
  replaceRoute,
  useRoute,
} from './router'
import { useConfigSnapshot } from './state/config'
import { useProjectWorkspace } from './state/projectWorkspace'
import { describeError } from './state/errors'
import { useWorkspace } from './state/workspaceContext'
import type { RouterState } from './api/types'
import type { ConnState } from './ws/sessionSocket'

const CONTEXT_WINDOW = Number(import.meta.env.VITE_ROUTIVUS_CONTEXT_WINDOW ?? 128000) || 128000

export function App() {
  const route = useRoute()
  const { projects, loading, error, refresh, theme, toggleTheme } = useWorkspace()
  const configState = useConfigSnapshot()

  const projectId = route.kind === 'project' ? route.projectId : null
  const routeSessionId = route.kind === 'project' ? route.sessionId : null
  const workspace = useProjectWorkspace(projectId, routeSessionId)

  const [terminalOpen, setTerminalOpen] = useState(false)
  const [hitl, setHitl] = useState<string | null>(null)
  const [router, setRouter] = useState<RouterState | null>(null)
  const [connection, setConnection] = useState<ConnState>('offline')
  const [globalNoteId, setGlobalNoteId] = useState<string | null>(null)
  const [projectNoteSel, setProjectNoteSel] = useState<Record<string, string | null>>({})
  const [newProjectOpen, setNewProjectOpen] = useState(false)

  useEffect(() => {
    if (route.kind !== 'project') {
      setTerminalOpen(false)
      setHitl(null)
      setRouter(null)
      setConnection('offline')
    }
  }, [route.kind])

  // Ctrl+` 折叠 / 展开终端（仅项目态）
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.ctrlKey && event.key === '`') {
        event.preventDefault()
        if (route.kind === 'project') setTerminalOpen((value) => !value)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [route.kind])

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
      case 'config':
        return (
          <ConfigView
            config={configState.config}
            loading={configState.loading}
            error={configState.error}
            onReload={configState.reload}
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
        return (
          <ChatView
            projectId={route.projectId}
            projectName={activeProject?.name ?? 'project'}
            projectPath={activeProject?.root_path ?? null}
            session={liveSession}
            projectNotesCount={workspace.projectNotes.length}
            terminalOpen={terminalOpen}
            contextWindow={CONTEXT_WINDOW}
            onToggleTerminal={() => setTerminalOpen((value) => !value)}
            onSessionUpdate={workspace.applySessionUpdate}
            onHitlChange={setHitl}
            onRouterChange={setRouter}
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
    projectNoteSel,
    globalNoteId,
    handleSelectNote,
    handleSelectProject,
    handleNotesMutated,
    configState,
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
        onSelectProjectNote={handleSelectProjectNote}
        onNewProjectNote={() => void handleNewProjectNote()}
        onGoHome={() => navigate(HOME)}
        onGoNotes={() => navigate(GLOBAL_NOTES)}
        onGoSkills={() => navigate(CONFIG)}
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
          contextWindow={CONTEXT_WINDOW}
          hitl={hitl}
          router={router}
          terminalOpen={terminalOpen}
          config={configState.config}
          onSwitchModel={handleSwitchModel}
          onToggleTheme={toggleTheme}
          onToggleTerminal={() => setTerminalOpen((value) => !value)}
          onGoHome={() => navigate(HOME)}
          onGoNotes={() => navigate(GLOBAL_NOTES)}
          onGoConfig={() => navigate(CONFIG)}
          onGoChat={() => {
            if (projectId) navigate(projectRoute(projectId, 'chat', workspace.activeSession?.id ?? null))
          }}
          onGoProjectNotes={() => {
            if (projectId) navigate(projectRoute(projectId, 'notes'))
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
