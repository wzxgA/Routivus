import { useCallback, useEffect, useState } from 'react'
import * as api from '../../api'
import type { FileContent } from '../../api/types'
import { describeError } from '../../state/errors'
import { Empty } from '../common/Empty'
import { FileEditor } from './FileEditor'
import { FilePreview } from './FilePreview'
import { FileTree } from './FileTree'

interface FilesViewProps {
  projectId: string
  projectName: string
  projectPath: string | null
}

/**
 * 项目工作区文件：左侧懒加载树 + 右侧编辑 / 预览。
 *
 * 只读的文件（二进制、超 1MB 截断、含无法解码字节）走预览分支；可编辑的走编辑器。
 * 所有写入都由服务端做路径校验并落审计，见 plans/enhancement/04-workspace-files.md。
 */
export function FilesView({ projectId, projectName, projectPath }: FilesViewProps) {
  const [selected, setSelected] = useState<string | null>(null)
  const [file, setFile] = useState<FileContent | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [treeKey, setTreeKey] = useState(0)
  const [creating, setCreating] = useState<'file' | 'dir' | null>(null)
  const [newPath, setNewPath] = useState('')
  const [createError, setCreateError] = useState('')

  const open = useCallback(
    async (path: string) => {
      setSelected(path)
      setLoading(true)
      setError('')
      try {
        setFile(await api.readProjectFile(projectId, path))
      } catch (caught) {
        setFile(null)
        setError(describeError(caught))
      } finally {
        setLoading(false)
      }
    },
    [projectId],
  )

  const reload = useCallback(() => {
    if (selected) void open(selected)
  }, [open, selected])

  // 切项目：清空选择与错误，避免显示上一个项目的文件
  useEffect(() => {
    setSelected(null)
    setFile(null)
    setError('')
    setCreating(null)
    setCreateError('')
  }, [projectId])

  const submitCreate = async () => {
    if (!creating) return
    const path = newPath.trim()
    if (!path) return
    setCreateError('')
    try {
      await api.createProjectEntry(projectId, { path, kind: creating })
      const kind = creating
      setCreating(null)
      setNewPath('')
      setTreeKey((value) => value + 1)
      if (kind === 'file') void open(path)
    } catch (caught) {
      setCreateError(describeError(caught))
    }
  }

  return (
    <section className="view view-page files-view">
      <div className="files-head">
        <div>
          <div className="page-title">{projectName}</div>
          {projectPath ? (
            <div className="page-sub" title={projectPath}>
              {projectPath}
            </div>
          ) : null}
        </div>
        <span className="spacer" />
        <button
          type="button"
          className="btn tiny"
          onClick={() => {
            setCreating('file')
            setNewPath('')
            setCreateError('')
          }}
        >
          新建文件
        </button>
        <button
          type="button"
          className="btn tiny"
          onClick={() => {
            setCreating('dir')
            setNewPath('')
            setCreateError('')
          }}
        >
          新建目录
        </button>
      </div>

      {creating ? (
        <div className="banner">
          <span>{creating === 'file' ? '新建文件' : '新建目录'}</span>
          <input
            className="fe-input"
            value={newPath}
            autoFocus
            placeholder="相对路径，例如 src/new.py（父目录需已存在）"
            onChange={(event) => setNewPath(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void submitCreate()
              if (event.key === 'Escape') setCreating(null)
            }}
          />
          <button type="button" className="link-btn" onClick={() => void submitCreate()}>
            创建
          </button>
          <button type="button" className="link-btn" onClick={() => setCreating(null)}>
            取消
          </button>
        </div>
      ) : null}
      {createError ? (
        <div className="banner error">
          <span>{createError}</span>
        </div>
      ) : null}

      <div className="files-body">
        <FileTree
          projectId={projectId}
          activePath={selected}
          onOpenFile={(path) => void open(path)}
          refreshKey={treeKey}
        />

        <div className="files-pane">
          {loading ? <div className="hint">加载中…</div> : null}
          {error ? (
            <div className="banner error">
              <span>{error}</span>
            </div>
          ) : null}
          {!loading && !file && !error ? (
            <Empty
              title="选择一个文件"
              hint="左侧是项目工作区里的真实文件；点开即可查看或编辑。所有写入都会连接到项目根的校验规则，并记入 .routivus/audit.log。"
            />
          ) : null}
          {file && !loading ? (
            file.editable ? (
              <FileEditor
                projectId={projectId}
                file={file}
                onSaved={() => {
                  void reload()
                  setTreeKey((value) => value + 1)
                }}
                onReload={reload}
              />
            ) : (
              <>
                <div className="fp-bar">
                  <span className="fe-path" title={file.path}>
                    {file.path}
                  </span>
                  <span className="fe-meta">
                    只读 · {(file.size / 1024).toFixed(1)} KB
                  </span>
                </div>
                <div className="files-scroll">
                  <FilePreview projectId={projectId} file={file} />
                </div>
              </>
            )
          ) : null}
        </div>
      </div>
    </section>
  )
}
