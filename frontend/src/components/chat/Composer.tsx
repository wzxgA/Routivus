import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchCompletions, searchProjectFiles } from '../../api'
import type { CompletionResponse, FileEntry, SessionStatus } from '../../api/types'
import { removeAtRef, splitAtSegments, uniqueRefPaths } from '../../utils/atRefs'

/** 发送模式：与后端 `/plan`、`/team` 前缀一一对应（见 `_parse_task_command`）。 */
export type ComposerMode = 'chat' | 'plan' | 'team'

const MODES: { id: ComposerMode; label: string; hint: string }[] = [
  { id: 'chat', label: '对话', hint: '直接执行，不生成计划' },
  { id: 'plan', label: '计划', hint: '先生成依赖图，审阅后按批次执行' },
  { id: 'team', label: '团队', hint: '多 Agent 协作，结果带证据化审查' },
]

const MODE_LABEL: Record<ComposerMode, string> = {
  chat: '对话',
  plan: '计划',
  team: '团队',
}

/** 文件候选右侧的大小说明（目录单独标）。 */
function formatSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

interface ComposerProps {
  prompt: string
  /** 当前会话所属项目：`@` 补全只认这个项目内的路径（方案 05 §2 非目标：不做跨项目）。 */
  projectId: string
  value: string
  history: string[]
  sessionId: string | null
  status: SessionStatus | null
  disabled: boolean
  waitingApproval: boolean
  onChange: (value: string) => void
  onSubmit: (mode: ComposerMode) => void
  onCancel: () => void
}

export function Composer({
  prompt,
  projectId,
  value,
  history,
  sessionId,
  status,
  disabled,
  waitingApproval,
  onChange,
  onSubmit,
  onCancel,
}: ComposerProps) {
  const [cursor, setCursor] = useState(-1)
  const [mode, setMode] = useState<ComposerMode>('chat')
  const [menuOpen, setMenuOpen] = useState(false)
  const [sugs, setSugs] = useState<CompletionResponse | null>(null)
  const [fileSugs, setFileSugs] = useState<FileEntry[] | null>(null)
  const [active, setActive] = useState(0)
  const [caret, setCaret] = useState(0)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const modeRef = useRef<HTMLDivElement | null>(null)

  const busy = status === 'running' || status === 'waiting_approval'

  /**
   * `@` 引用上下文：光标前最后一个 token 形如 `@前缀`，且 `@` 前是行首或空白。
   *
   * 与后端白名单同规则（方案 05 §3.2）——前缀以 `/`、`\`、引号开头或含 `..` 时
   * 不算引用，避免把 `@/abs` 这类输错形态发出去问一次注定无结果的搜索。
   */
  const atContext = useMemo(() => {
    const before = value.slice(0, caret)
    // 字符类与 `utils/atRefs.ts` 的 AT_REF_RE 一致：标点即边界（`@a.ts、` 不搜 `a.ts、`）
    const match = /(^|[\s，。、；：！？（）【】《》“”‘’"',;:!?])@([^\s@，。、；：！？（）【】《》“”‘’"',;:!?]*)$/.exec(before)
    if (!match) return null
    const query = match[2]
    if (query.startsWith('/') || query.startsWith('\\') || query.startsWith('"')) return null
    if (query.split('/').some((part) => part === '..')) return null
    return { start: before.length - query.length - 1, end: before.length, query }
  }, [value, caret])
  const atQuery = atContext?.query ?? null

  // chip 列表与高亮都从**文本**派生，不另存一份"插入历史"：手改文本后 chip 自动
  // 跟着变，两边不存在各自记账、互相漂移的可能（这是 A+B 优于纯文本方案的关键）。
  const refs = useMemo(() => uniqueRefPaths(value), [value])
  const segments = useMemo(() => splitAtSegments(value), [value])

  // 点菜单外部关闭（与顶栏模型选择器同构）
  useEffect(() => {
    if (!menuOpen) return
    const handler = (event: MouseEvent) => {
      if (modeRef.current && !modeRef.current.contains(event.target as Node)) setMenuOpen(false)
    }
    window.addEventListener('mousedown', handler)
    return () => window.removeEventListener('mousedown', handler)
  }, [menuOpen])

  // Composer 不可用时不要让菜单悬着
  useEffect(() => {
    if (disabled || busy) setMenuOpen(false)
  }, [disabled, busy])

  // 命令补全：输入以 "/" 开头时防抖拉取候选；网络失败静默降级为无提示。
  // 与文件补全**互斥**：光标正处在 `@` 上下文时不开命令浮层（方案 05 §5.3），
  // 否则 `/plan @src` 这种输入会两个浮层同时抢方向键。
  useEffect(() => {
    if (disabled || busy || atQuery !== null || !value.startsWith('/')) {
      setSugs(null)
      return
    }
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      fetchCompletions(value, caret, sessionId, controller.signal)
        .then((data) => {
          if (!data.is_command || data.candidates.length === 0) {
            setSugs(null)
            return
          }
          setSugs(data)
          setActive((index) => Math.min(index, data.candidates.length - 1))
        })
        .catch((error: unknown) => {
          // 主动 abort 的旧请求不算错误；真实失败也只降级，不打扰输入。
          if (!controller.signal.aborted) setSugs(null)
          void error
        })
    }, 120)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [value, caret, disabled, busy, sessionId, atQuery])

  // `@` 文件补全：光标停在 `@前缀` 上时按前缀搜项目内路径（方案 05 §5.1）。
  // 空前缀也照发 —— 后端只回**根下一层**（不递归），所以"敲一个 @ 就看到顶层
  // 文件"很便宜，也是这个功能最容易被发现的地方。
  useEffect(() => {
    if (disabled || busy || atQuery === null) {
      setFileSugs(null)
      return
    }
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      searchProjectFiles(projectId, atQuery, 20, controller.signal)
        .then((data) => {
          setFileSugs(data.entries)
          setActive((index) => Math.min(index, Math.max(0, data.entries.length - 1)))
        })
        .catch(() => {
          // 搜索失败只让浮层消失，不影响输入本身
          if (!controller.signal.aborted) setFileSugs(null)
        })
    }, 120)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [atQuery, disabled, busy, projectId])

  // textarea 随内容长高（超过约 5 行后内部滚动），避免多行输入被裁掉。
  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`
  }, [value])

  /** 应用一条候选：按服务端给出的 token 区间替换，光标落在插入文本末尾。 */
  const applySug = (index: number) => {
    if (!sugs) return
    const cand = sugs.candidates[index]
    if (!cand) return
    const start = Math.min(sugs.replace_start, value.length)
    const end = Math.min(sugs.replace_end, value.length)
    const next = value.slice(0, start) + cand.insert_text + value.slice(end)
    const nextCaret = start + cand.insert_text.length
    onChange(next)
    setSugs(null)
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (!el) return
      el.focus()
      el.setSelectionRange(nextCaret, nextCaret)
      setCaret(nextCaret)
    })
  }

  const syncCaret = () => setCaret(inputRef.current?.selectionStart ?? 0)

  /**
   * 应用一条文件候选：目录补 `/`（可继续逐级下钻），文件补一个空格（收尾）。
   * 替换区间就是 `@` 到光标那一段，不会误伤前面的正文。
   */
  const applyFileSug = (index: number) => {
    const entry = fileSugs?.[index]
    if (!entry || !atContext) return
    const insert = entry.type === 'dir' ? `@${entry.path}/` : `@${entry.path} `
    const next = value.slice(0, atContext.start) + insert + value.slice(atContext.end)
    const nextCaret = atContext.start + insert.length
    onChange(next)
    setFileSugs(null)
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (!el) return
      el.focus()
      el.setSelectionRange(nextCaret, nextCaret)
      setCaret(nextCaret)
    })
  }

  /** 摘掉这个路径的全部引用（chip 上的 ×）。文本是唯一真相，删完 chip 自动同步。 */
  const removeRef = (path: string) => {
    onChange(removeAtRef(value, path))
  }

  const submit = () => {
    const text = value.trim()
    // 空 / 单独一个 "/" 不发送：后者多半是命令还没敲完（补全浮层正开着），
    // 发出去只会换来一张"命令失败"卡。
    if (busy || disabled || !text || text === '/') return
    // 模式单次生效：交给上层包装成 /plan、/team 后立即回到对话模式
    onSubmit(mode)
    setMode('chat')
    setMenuOpen(false)
    setCursor(-1)
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // 补全浮层打开时：↑↓ 在候选间移动（不再翻历史），Tab 应用，Esc 关闭。
    // Enter 仍发送 —— 补全只提示不拦截，避免打断“敲完直接回车”的习惯。
    if (fileSugs && fileSugs.length > 0) {
      if (event.key === 'Escape') {
        event.preventDefault()
        setFileSugs(null)
        return
      }
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        setActive((index) => (index + 1) % fileSugs.length)
        return
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault()
        setActive((index) => (index - 1 + fileSugs.length) % fileSugs.length)
        return
      }
      if (event.key === 'Tab') {
        event.preventDefault()
        applyFileSug(active)
        return
      }
    }
    if (sugs && sugs.candidates.length > 0) {
      if (event.key === 'Escape') {
        event.preventDefault()
        setSugs(null)
        return
      }
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        setActive((index) => (index + 1) % sugs.candidates.length)
        return
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault()
        setActive((index) => (index - 1 + sugs.candidates.length) % sugs.candidates.length)
        return
      }
      if (event.key === 'Tab') {
        event.preventDefault()
        applySug(active)
        return
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
      return
    }
    if (event.key === 'ArrowUp') {
      if (history.length === 0) return
      event.preventDefault()
      const next = cursor === -1 ? history.length - 1 : Math.max(0, cursor - 1)
      setCursor(next)
      onChange(history[next])
      return
    }
    if (event.key === 'ArrowDown') {
      if (history.length === 0 || cursor === -1) return
      event.preventDefault()
      const next = cursor + 1
      if (next >= history.length) {
        setCursor(-1)
        onChange('')
      } else {
        setCursor(next)
        onChange(history[next])
      }
    }
  }

  const placeholder = waitingApproval
    ? '等待审批决策…（在上方审批卡中选择动作）'
    : busy
      ? 'Agent 正在执行…'
      : mode === 'plan'
        ? '描述要规划的任务，Enter 发送（计划模式）'
        : mode === 'team'
          ? '描述要交给多个 Agent 的任务，Enter 发送（团队模式）'
          : '输入任务或 / 命令，@ 引用文件，Enter 发送，↑↓ 调历史'

  return (
    <div className="composer-wrap">
      {/* 已引用文件（B）：chip 由**文本**派生，× 只是把对应 `@路径` 从文本里删掉，
          因此不存在"列表与正文不一致"的中间态。 */}
      {refs.length > 0 ? (
        <div className="cprefs">
          <span className="cprefs-label">已引用</span>
          {refs.map((path) => (
            <span className="cpref" key={path}>
              <span className="cpref-name" title={path}>
                {path}
              </span>
              <button
                type="button"
                className="cpref-x"
                title="移除引用"
                aria-label={`移除引用 ${path}`}
                // mousedown 先于 blur：阻止失焦才能把光标留在输入框里
                onMouseDown={(event) => {
                  event.preventDefault()
                  removeRef(path)
                }}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      ) : null}

      <div className="composer">
        <span className="composer-prompt">{prompt} &gt;</span>

        <div className={`pmode${menuOpen ? ' open' : ''}`} ref={modeRef}>
          <button
            type="button"
            className={`pmode-btn${mode === 'chat' ? '' : ' active'}`}
            disabled={disabled || busy}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            title="选择发送模式"
            onClick={() => setMenuOpen((open) => !open)}
          >
            <span>{MODE_LABEL[mode]}</span>
            <span className="pmode-caret">▼</span>
          </button>
          <div className="pmode-pop" role="menu">
            <div className="pop-title">发送模式</div>
            {MODES.map((item) => (
              <button
                type="button"
                key={item.id}
                role="menuitemradio"
                aria-checked={mode === item.id}
                className="pmode-item"
                onClick={() => {
                  setMode(item.id)
                  setMenuOpen(false)
                  inputRef.current?.focus()
                }}
              >
                <span className="pmode-name">{item.label}</span>
                <span className="pmode-desc">{item.hint}</span>
                {mode === item.id ? <span className="pmode-tick">●</span> : null}
              </button>
            ))}
          </div>
        </div>

        {/* A：高亮层在下、textarea 在上，两层共用同一套字体与行盒参数（见 app.css
            的 .cinput-* 注释），所以底纹不会跑位。高亮层只画背景，文字仍由
            textarea 自己显示，因此选区、光标、IME 全是原生行为。 */}
        <div className="cinput">
          <div className="cinput-hl" aria-hidden="true">
            {segments.map((segment, index) =>
              segment.ref ? (
                <mark key={index}>{segment.text}</mark>
              ) : (
                <span key={index}>{segment.text}</span>
              ),
            )}
            {'\n'}
          </div>
          <textarea
            ref={inputRef}
            rows={1}
            value={value}
            placeholder={placeholder}
            disabled={disabled || busy}
            onChange={(event) => {
              onChange(event.target.value)
              // 受控组件里 DOM 已更新：直接取新光标，补全上下文不必等下一次事件
              setCaret(event.target.selectionStart ?? 0)
            }}
            onKeyDown={handleKeyDown}
            onSelect={syncCaret}
            onClick={syncCaret}
            onKeyUp={syncCaret}
            autoComplete="off"
            spellCheck={false}
          />
        </div>
        {fileSugs && fileSugs.length > 0 ? (
          <div className="csugs" role="listbox" aria-label="文件引用补全">
            <div className="csugs-title">Tab 引用 · Esc 关闭</div>
            {fileSugs.map((entry, index) => (
              <button
                type="button"
                key={entry.path}
                role="option"
                aria-selected={index === active}
                className={`csug${index === active ? ' active' : ''}`}
                onMouseDown={(event) => {
                  event.preventDefault()
                  applyFileSug(index)
                }}
                onMouseEnter={() => setActive(index)}
              >
                <span className="csug-name">
                  {entry.path}
                  {entry.type === 'dir' ? '/' : ''}
                </span>
                <span className="csug-desc">
                  {entry.type === 'dir' ? '目录' : formatSize(entry.size)}
                </span>
              </button>
            ))}
          </div>
        ) : null}
        {sugs && sugs.candidates.length > 0 ? (
          <div className="csugs" role="listbox" aria-label="命令补全">
            <div className="csugs-title">Tab 应用 · Esc 关闭</div>
            {sugs.candidates.map((cand, index) => (
              <button
                type="button"
                key={`${cand.insert_text}-${index}`}
                role="option"
                aria-selected={index === active}
                className={`csug${index === active ? ' active' : ''}`}
                // mousedown 先于 textarea blur：阻止失焦才能保住光标与焦点
                onMouseDown={(event) => {
                  event.preventDefault()
                  applySug(index)
                }}
                onMouseEnter={() => setActive(index)}
              >
                <span className="csug-name">{cand.label}</span>
                <span className="csug-desc">{cand.detail}</span>
              </button>
            ))}
          </div>
        ) : null}
        {busy ? (
          <button type="button" className="btn tiny" onClick={onCancel}>
            停止
          </button>
        ) : (
          <button
            type="button"
            className="btn tiny primary"
            disabled={disabled || !value.trim()}
            onClick={submit}
          >
            发送
          </button>
        )}
      </div>
    </div>
  )
}
