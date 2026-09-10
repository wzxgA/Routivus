import { useEffect, useRef, useState } from 'react'
import type { Project, Session } from '../../api/types'
import type { Route } from '../../router'
import type { ThemeName } from '../../theme'
import type { ConnState } from '../../ws/sessionSocket'
import { ThemeToggle } from '../common/ThemeToggle'

interface TopBarProps {
  route: Route
  project: Project | null
  liveSession: Session | null
  projectNotesCount: number
  connection: ConnState
  theme: ThemeName
  contextWindow: number
  hitl: string | null
  terminalOpen: boolean
  onToggleTheme: () => void
  onToggleTerminal: () => void
  onGoHome: () => void
  onGoNotes: () => void
  onGoConfig: () => void
  onGoChat: () => void
  onGoProjectNotes: () => void
}

const STATUS_TEXT: Record<string, string> = {
  idle: '空闲',
  running: '运行中',
  waiting_approval: '等待审批',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

function ConnectionBadge({ state }: { state: ConnState }) {
  if (state === 'connected' || state === 'connecting') return null
  const offline = state === 'offline'
  return (
    <div className={`conn-badge${offline ? ' offline' : ''}`}>
      <span className="dot" />
      {offline ? '连接断开' : '重连中…'}
    </div>
  )
}

export function TopBar({
  route,
  project,
  liveSession,
  projectNotesCount,
  connection,
  theme,
  contextWindow,
  hitl,
  terminalOpen,
  onToggleTheme,
  onToggleTerminal,
  onGoHome,
  onGoNotes,
  onGoConfig,
  onGoChat,
  onGoProjectNotes,
}: TopBarProps) {
  const [modelOpen, setModelOpen] = useState(false)
  const modelRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!modelOpen) return
    const handler = (event: MouseEvent) => {
      if (modelRef.current && !modelRef.current.contains(event.target as Node)) setModelOpen(false)
    }
    window.addEventListener('mousedown', handler)
    return () => window.removeEventListener('mousedown', handler)
  }, [modelOpen])

  const inProject = route.kind === 'project'
  const usageRatio =
    liveSession && contextWindow > 0
      ? Math.min(1, liveSession.total_tokens / contextWindow)
      : 0
  const status = liveSession?.status ?? 'idle'

  return (
    <header className="topbar">
      {inProject ? (
        <>
          <div className="proj-crumb">
            <b>{project?.name ?? '项目'}</b>
            <span className="crumb-path" title={project?.root_path}>
              {project?.root_path ?? ''}
            </span>
          </div>
          <div className="vtabs">
            <button
              type="button"
              className={`vtab${route.view === 'chat' ? ' active' : ''}`}
              onClick={onGoChat}
            >
              会话
            </button>
            <button
              type="button"
              className={`vtab${route.view === 'notes' ? ' active' : ''}`}
              onClick={onGoProjectNotes}
            >
              笔记
              {projectNotesCount > 0 ? ` ${projectNotesCount}` : ''}
            </button>
          </div>
          <button
            type="button"
            className={`term-toggle${terminalOpen ? ' on' : ''}`}
            onClick={onToggleTerminal}
            title="折叠 / 展开终端（Ctrl+`）"
          >
            <span className="term-ic">&gt;_</span>终端
          </button>
          <div className="chips">
            {status === 'running' || status === 'waiting_approval' ? (
              <span className="chip">
                <span className="dot" />
                {STATUS_TEXT[status]}
              </span>
            ) : (
              <span className="chip">{STATUS_TEXT[status] ?? status}</span>
            )}
            <span className="chip" title="上下文使用率">
              Context <span className="num">{(usageRatio * 100).toFixed(1)}%</span>
            </span>
            <span className="chip hitl" title="HITL 由服务端托管">
              {hitl ? 'HITL ON' : 'HITL —'}
            </span>
          </div>
          <ConnectionBadge state={connection} />
          <div className={`model${modelOpen ? ' open' : ''}`} ref={modelRef}>
            <button type="button" className="model-btn" onClick={() => setModelOpen((v) => !v)}>
              <span className="model-name">{liveSession?.active_model || '未配置模型'}</span>
              <span className="model-provider">{liveSession?.active_provider || ''}</span>
              <span className="model-caret">▼</span>
            </button>
            <div className="pop">
              <div className="pop-title">当前会话模型</div>
              <div className="pop-item">
                <span className="m">{liveSession?.active_model || '未配置'}</span>
                <span className="v">
                  {liveSession?.active_provider || '—'}
                  {liveSession ? ' · 当前' : ''}
                </span>
              </div>
              <div className="hint" style={{ margin: '6px 4px 2px' }}>
                模型切换需由配置接口提供 provider / 四档数据，当前后端尚未暴露该接口。
              </div>
            </div>
          </div>
        </>
      ) : (
        <>
          <div className="vtabs">
            <button
              type="button"
              className={`vtab${route.kind === 'home' ? ' active' : ''}`}
              onClick={onGoHome}
            >
              首页
            </button>
            <button
              type="button"
              className={`vtab${route.kind === 'notes' ? ' active' : ''}`}
              onClick={onGoNotes}
            >
              笔记
            </button>
            <button
              type="button"
              className={`vtab${route.kind === 'config' ? ' active' : ''}`}
              onClick={onGoConfig}
            >
              配置
            </button>
          </div>
        </>
      )}

      {/* 项目态下模型选择器自带 margin-left:auto，无需再补占位，避免出现双段空白 */}
      {inProject ? null : <div style={{ marginLeft: 'auto' }} />}
      <ThemeToggle theme={theme} onToggle={onToggleTheme} />
    </header>
  )
}
