import { Fragment, useCallback, useEffect, useState, type ReactNode } from 'react'
import * as api from '../../api'
import type { FileEntry } from '../../api/types'
import { describeError } from '../../state/errors'

const ROOT = ''

interface FileTreeProps {
  projectId: string
  /** 当前打开的文件（高亮用）。 */
  activePath: string | null
  onOpenFile: (path: string) => void
  /** 外部触发的刷新（新建之后自增即可）。 */
  refreshKey?: number
  /** 抽屉里用更紧凑的行高。 */
  compact?: boolean
}

/**
 * 懒加载文件树：展开一个目录才请求一层（不做整树扫描，大仓库也不卡）。
 *
 * 已加载的层缓存在组件内，来回展开不重复请求；`refreshKey` 变化即清空重来。
 * 忽略目录（.git / node_modules / dist…）默认不列出，由"含忽略"开关控制。
 */
export function FileTree({
  projectId,
  activePath,
  onOpenFile,
  refreshKey = 0,
  compact = false,
}: FileTreeProps) {
  const [children, setChildren] = useState<Record<string, FileEntry[]>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({ [ROOT]: true })
  const [loading, setLoading] = useState<Record<string, boolean>>({})
  const [error, setError] = useState<Record<string, string>>({})
  const [includeIgnored, setIncludeIgnored] = useState(false)

  const load = useCallback(
    async (path: string, withIgnored: boolean) => {
      setLoading((current) => ({ ...current, [path]: true }))
      try {
        const listing = await api.listProjectFiles(projectId, path, {
          includeIgnored: withIgnored,
        })
        setChildren((current) => ({ ...current, [path]: listing.entries }))
        setError((current) => ({
          ...current,
          [path]: listing.truncated ? `条目超过 ${listing.limit} 个，已截断` : '',
        }))
      } catch (caught) {
        setError((current) => ({ ...current, [path]: describeError(caught) }))
      } finally {
        setLoading((current) => ({ ...current, [path]: false }))
      }
    },
    [projectId],
  )

  // 换项目 / 外部刷新 / 切换"含忽略"：清空缓存，回到只展开根的状态
  useEffect(() => {
    setChildren({})
    setExpanded({ [ROOT]: true })
    setError({})
    void load(ROOT, includeIgnored)
  }, [projectId, refreshKey, includeIgnored, load])

  const toggle = useCallback(
    (entry: FileEntry) => {
      const willExpand = !expanded[entry.path]
      setExpanded((current) => ({ ...current, [entry.path]: willExpand }))
      if (willExpand && children[entry.path] === undefined) {
        void load(entry.path, includeIgnored)
      }
    },
    [children, expanded, includeIgnored, load],
  )

  const renderLevel = (path: string, depth: number): ReactNode => {
    const entries = children[path]
    if (!entries) return null
    return entries.map((entry) => {
      const isDir = entry.type === 'dir'
      const open = Boolean(expanded[entry.path])
      const indent = 8 + depth * 13
      return (
        <Fragment key={entry.path}>
          <button
            type="button"
            className={`ft-row${entry.path === activePath ? ' active' : ''}${
              compact ? ' compact' : ''
            }${entry.outside ? ' muted' : ''}`}
            style={{ paddingLeft: indent }}
            title={entry.path}
            onClick={() => (isDir ? toggle(entry) : onOpenFile(entry.path))}
          >
            <span className="ft-caret">{isDir ? (open ? '▾' : '▸') : ''}</span>
            <span className="ft-name">{entry.name}</span>
            {entry.outside ? <span className="ft-flag">域外</span> : null}
            {entry.symlink && !entry.outside ? <span className="ft-flag">链接</span> : null}
            {entry.ignored ? <span className="ft-flag">忽略</span> : null}
          </button>
          {error[entry.path] ? (
            <div className="ft-note error" style={{ paddingLeft: 8 + (depth + 1) * 13 }}>
              {error[entry.path]}
            </div>
          ) : null}
          {isDir && open && children[entry.path] === undefined ? (
            <div className="ft-note" style={{ paddingLeft: 8 + (depth + 1) * 13 }}>
              {loading[entry.path] ? '加载中…' : '空目录'}
            </div>
          ) : null}
          {isDir && open ? renderLevel(entry.path, depth + 1) : null}
        </Fragment>
      )
    })
  }

  return (
    <div className={`file-tree${compact ? ' compact' : ''}`}>
      <div className="ft-bar">
        <button type="button" className="link-btn" onClick={() => void load(ROOT, includeIgnored)}>
          刷新
        </button>
        <label className="ft-toggle" title="显示 .git / node_modules / dist 等被忽略的目录">
          <input
            type="checkbox"
            checked={includeIgnored}
            onChange={(event) => setIncludeIgnored(event.target.checked)}
          />
          含忽略
        </label>
      </div>
      <div className="ft-scroll">
        {error[ROOT] ? <div className="ft-note error">{error[ROOT]}</div> : null}
        {children[ROOT] === undefined && loading[ROOT] ? (
          <div className="ft-note">加载中…</div>
        ) : null}
        {children[ROOT]?.length === 0 ? <div className="ft-note">空目录</div> : null}
        {renderLevel(ROOT, 0)}
      </div>
    </div>
  )
}
