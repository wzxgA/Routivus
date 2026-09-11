import { useCallback, useEffect, useState } from 'react'
import * as api from '../../api'
import type { Project, SkillDetail, SkillView } from '../../api/types'
import { describeError } from '../../state/errors'
import { Modal } from '../common/Modal'

/**
 * 技能页：Skill 的独立管理界面（列表 / 预览 / 编辑 / 启停）。
 *
 * 顶部项目选择器决定上下文：`全局`=内置 + 用户级 Skill；选中项目后额外包含该项目
 * `.routivus/skills` 下的项目级 Skill，新建时也可选择写入项目层。
 * 与配置页解耦，与会话内 `/skill` 命令共用同一份配置（skills.json）。
 */

const SOURCE_LABEL: Record<string, string> = {
  builtin: '内置',
  user: '用户级',
  project: '项目级',
}

interface SkillsViewProps {
  projects: Project[]
}

export function SkillsView({ projects }: SkillsViewProps) {
  const [scope, setScope] = useState('') // '' = 全局（用户级 + 内置）
  const [skills, setSkills] = useState<SkillView[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<SkillDetail | null>(null)
  const [editor, setEditor] = useState<{ detail: SkillDetail | null } | null>(null)

  const projectId = scope || undefined
  const scopedProject = projects.find((item) => item.id === scope) ?? null

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setSkills(await api.listSkills(projectId))
      setError(null)
    } catch (err) {
      setError(describeError(err))
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    void load()
  }, [load])

  const toggle = useCallback(
    async (skill: SkillView) => {
      setBusy(true)
      setError(null)
      try {
        setSkills(await api.setSkillEnabled(skill.name, !skill.enabled, projectId))
      } catch (err) {
        setError(describeError(err))
      } finally {
        setBusy(false)
      }
    },
    [projectId],
  )

  const openPreview = useCallback(
    async (skill: SkillView) => {
      setError(null)
      try {
        setPreview(await api.getSkill(skill.name, projectId))
      } catch (err) {
        setError(describeError(err))
      }
    },
    [projectId],
  )

  const openEditor = useCallback(
    async (skill: SkillView) => {
      setError(null)
      try {
        setEditor({ detail: await api.getSkill(skill.name, projectId) })
      } catch (err) {
        setError(describeError(err))
      }
    },
    [projectId],
  )

  return (
    <section className="view view-page">
      <div className="page-title">技能</div>
      <div className="page-sub">
        只读任务规范：索引随系统提示注入，正文由模型按需通过 <code>load_skill</code> 加载。
      </div>

      {error ? (
        <div className="banner error">
          <span>{error}</span>
          <span className="spacer" />
          <button type="button" className="link-btn" onClick={() => setError(null)}>
            关闭
          </button>
        </div>
      ) : null}

      <div className="big-card">
        <div className="card-head">
          <div className="card-name">范围</div>
          <span className="task-acts">
            <select
              className="ask-select"
              value={scope}
              disabled={busy}
              onChange={(event) => setScope(event.target.value)}
            >
              <option value="">全局（内置 + 用户级）</option>
              {projects.map((item) => (
                <option key={item.id} value={item.id}>
                  项目：{item.name}
                </option>
              ))}
            </select>
            <button type="button" className="btn" disabled={loading || busy} onClick={() => void load()}>
              刷新
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={busy}
              onClick={() => setEditor({ detail: null })}
            >
              + 新建 Skill
            </button>
          </span>
        </div>
        <div className="card-desc">
          {scopedProject ? (
            <>
              当前范围包含 <span className="mono">{scopedProject.root_path}/.routivus/skills</span>
              ；新建时可选择写入用户级或该项目。
            </>
          ) : (
            <>
              存放位置：<span className="mono">&lt;用户目录&gt;/skills/&lt;名称&gt;/SKILL.md</span>
              ；选择项目后可查看与新建项目级 Skill。
            </>
          )}
        </div>

        <table className="table">
          <thead>
            <tr>
              <th>名称</th>
              <th>说明</th>
              <th>来源</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {skills.map((skill) => (
              <tr key={`${skill.source}-${skill.name}`}>
                <td className="mono">{skill.name}</td>
                <td>{skill.description || '—'}</td>
                <td>{SOURCE_LABEL[skill.source] ?? skill.source}</td>
                <td>
                  {skill.valid ? (
                    skill.enabled ? (
                      <span className="pill-ok">已启用</span>
                    ) : (
                      '已禁用'
                    )
                  ) : (
                    `无效：${skill.error}`
                  )}
                </td>
                <td>
                  <span className="task-acts">
                    <button type="button" className="btn tiny" onClick={() => void openPreview(skill)}>
                      预览
                    </button>
                    <button
                      type="button"
                      className="btn tiny"
                      disabled={skill.source === 'builtin'}
                      onClick={() => void openEditor(skill)}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      className="btn tiny"
                      disabled={busy || !skill.valid}
                      onClick={() => void toggle(skill)}
                    >
                      {skill.enabled ? '禁用' : '启用'}
                    </button>
                  </span>
                </td>
              </tr>
            ))}
            {skills.length === 0 ? (
              <tr>
                <td colSpan={5}>
                  {loading ? '正在加载 Skill…' : '该范围下没有发现 Skill。'}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
        <div className="hint">
          Skill 内容是补充资料，不能覆盖系统提示、安全策略、HITL 或工具权限；参考资料只能放在
          Skill 目录的 <code>references/</code> 下。
        </div>
      </div>

      {preview ? (
        <Modal
          title={`预览：${preview.name}`}
          description={`${SOURCE_LABEL[preview.source] ?? preview.source} · ${preview.path}`}
          onClose={() => setPreview(null)}
          actions={
            <button type="button" className="btn" onClick={() => setPreview(null)}>
              关闭
            </button>
          }
        >
          <pre className="skill-preview">{preview.body || '（正文为空或不可读）'}</pre>
        </Modal>
      ) : null}

      {editor ? (
        <SkillEditorModal
          detail={editor.detail}
          projectName={scopedProject?.name ?? null}
          busy={busy}
          onClose={() => setEditor(null)}
          onSubmit={(name, payload) => {
            setBusy(true)
            setError(null)
            void api
              .saveSkill(name, payload, projectId)
              .then(async () => {
                setEditor(null)
                await load()
              })
              .catch((err: unknown) => setError(describeError(err)))
              .finally(() => setBusy(false))
          }}
        />
      ) : null}
    </section>
  )
}

const SKILL_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

function SkillEditorModal({
  detail,
  projectName,
  busy,
  onClose,
  onSubmit,
}: {
  detail: SkillDetail | null
  projectName: string | null
  busy: boolean
  onClose: () => void
  onSubmit: (name: string, payload: { body: string; description: string; layer: 'user' | 'project' }) => void
}) {
  const editing = detail !== null
  const [name, setName] = useState(detail?.name ?? '')
  const [description, setDescription] = useState(detail?.description ?? '')
  const [body, setBody] = useState(detail?.body ?? '')
  const [layer, setLayer] = useState<'user' | 'project'>('user')

  const nameOk = SKILL_NAME_RE.test(name)
  const canSubmit = nameOk && body.trim().length > 0

  return (
    <Modal
      title={editing ? `编辑 Skill：${detail?.name}` : '新建 Skill'}
      description={
        editing
          ? `写入 ${detail?.layer === 'project' ? '项目级' : '用户级'}：${detail?.path}`
          : '名称只能用小写字母、数字、短横线与下划线；正文即 SKILL.md 的规范内容。'
      }
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={busy || !canSubmit}
            onClick={() => onSubmit(name.trim(), { body, description: description.trim(), layer })}
          >
            {busy ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      <div className="field">
        <label htmlFor="sk-name">名称</label>
        <input
          id="sk-name"
          className="mono"
          value={name}
          disabled={editing || busy}
          placeholder="api-conventions"
          onChange={(event) => setName(event.target.value.trim())}
        />
        {!editing && name && !nameOk ? (
          <div className="hint">名称只能包含小写字母、数字、短横线和下划线（1～64 位）。</div>
        ) : null}
      </div>

      {editing ? null : (
        <div className="field">
          <label htmlFor="sk-layer">写入位置</label>
          <select
            id="sk-layer"
            className="ask-select"
            value={layer}
            disabled={busy}
            onChange={(event) => setLayer(event.target.value === 'project' ? 'project' : 'user')}
          >
            <option value="user">用户级（所有项目可用）</option>
            <option value="project" disabled={!projectName}>
              {projectName ? `项目级（${projectName}）` : '项目级（需先在上方选择项目）'}
            </option>
          </select>
        </div>
      )}

      <div className="field">
        <label htmlFor="sk-desc">说明（可选，用于列表与索引）</label>
        <input
          id="sk-desc"
          value={description}
          disabled={busy}
          maxLength={200}
          onChange={(event) => setDescription(event.target.value)}
        />
      </div>

      <div className="field">
        <label htmlFor="sk-body">正文（Markdown）</label>
        <textarea
          id="sk-body"
          className="skill-editor"
          value={body}
          disabled={busy}
          rows={14}
          spellCheck={false}
          placeholder={'# 规范标题\n\n- 约束一\n- 约束二'}
          onChange={(event) => setBody(event.target.value)}
        />
      </div>
    </Modal>
  )
}
