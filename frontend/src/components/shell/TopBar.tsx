import { useEffect, useRef, useState } from 'react'
import type { ConfigSnapshot, Project, RouterState, Session } from '../../api/types'
import type { Route } from '../../router'
import { describeError } from '../../state/errors'
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
  router: RouterState | null
  terminalOpen: boolean
  config: ConfigSnapshot | null
  onSwitchModel: (provider: string, model: string) => Promise<void>
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
  router,
  terminalOpen,
  config,
  onSwitchModel,
  onToggleTheme,
  onToggleTerminal,
  onGoHome,
  onGoNotes,
  onGoConfig,
  onGoChat,
  onGoProjectNotes,
}: TopBarProps) {
  const [modelOpen, setModelOpen] = useState(false)
  const [switchError, setSwitchError] = useState<string | null>(null)
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

  // 智能路由开启时，会话里的 active_* 仍是「手动配置的模型」，本轮真正跑的是路由
  // 结果，所以优先展示路由档位，避免顶栏滞后于实际执行的模型。
  const routed = router?.enabled && router.model ? router : null
  const activeProvider = routed?.provider || liveSession?.active_provider || config?.active_provider || ''
  const currentModel = routed?.model || liveSession?.active_model || config?.active_model || ''
  const providerEntry = config?.providers.find((item) => item.name === activeProvider)
  const availableModels = providerEntry
    ? Array.from(
        new Set(
          [currentModel, providerEntry.default_model, ...providerEntry.models].filter(
            (value): value is string => Boolean(value),
          ),
        ),
      )
    : currentModel
      ? [currentModel]
      : []

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
            {router?.enabled ? (
              <span
                className={`chip router${router.error ? ' warn' : ''}`}
                title={
                  router.error ||
                  `智能路由：普通对话轮按复杂度自动换档${
                    router.tier ? `，本轮 ${router.tier}` : ''
                  }${router.configured ? '' : '（该档未显式配置，回落 active 模型）'}`
                }
              >
                SmartRouter {router.error ? '!' : router.tier || 'ON'}
              </span>
            ) : null}
          </div>
          <ConnectionBadge state={connection} />
          <div className={`model${modelOpen ? ' open' : ''}`} ref={modelRef}>
            <button
              type="button"
              className="model-btn"
              onClick={() => {
                setSwitchError(null)
                setModelOpen((open) => !open)
              }}
            >
              <span className="model-name">{currentModel || '未配置模型'}</span>
              <span className="model-provider">{activeProvider}</span>
              <span className="model-caret">▼</span>
            </button>
            <div className="pop">
              <div className="pop-title">运行模型</div>
              {availableModels.length === 0 ? (
                <div className="hint" style={{ margin: '6px 4px 2px' }}>
                  尚未配置 provider，请先到「配置」页添加。
                </div>
              ) : (
                availableModels.map((model) => (
                  <button
                    type="button"
                    key={model}
                    className="pop-item"
                    disabled={model === currentModel}
                    onClick={async () => {
                      setSwitchError(null)
                      try {
                        await onSwitchModel(activeProvider, model)
                        setModelOpen(false)
                      } catch (err) {
                        setSwitchError(describeError(err))
                      }
                    }}
                  >
                    <span className="m">{model}</span>
                    <span className="v">{model === currentModel ? '当前' : activeProvider}</span>
                  </button>
                ))
              )}
              {switchError ? (
                <div className="hint" style={{ margin: '6px 4px 2px', color: 'var(--accent)' }}>
                  {switchError}
                </div>
              ) : null}
              <div className="hint" style={{ margin: '6px 4px 2px' }}>
                切换的是全局默认模型；会话会复用已建立的 Agent，需新建会话后才生效。
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
