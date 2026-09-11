import { useCallback, useEffect, useMemo, useState } from 'react'
import * as api from '../../api'
import type { ConfigSnapshot, ProviderView, SkillView } from '../../api/types'
import { describeError } from '../../state/errors'
import { Modal } from '../common/Modal'

interface ConfigViewProps {
  config: ConfigSnapshot | null
  loading: boolean
  error: string | null
  onReload: () => Promise<ConfigSnapshot | null>
}

const SKILL_SOURCE_LABEL: Record<string, string> = {
  builtin: '内置',
  user: '用户级',
  project: '项目级',
}

type ModalState =
  | { kind: 'none' }
  | { kind: 'add-provider' }
  | { kind: 'edit-provider'; provider: ProviderView }
  | { kind: 'key'; provider: ProviderView }
  | { kind: 'delete-provider'; provider: ProviderView }
  | { kind: 'tier'; tier: string }

export function ConfigView({ config, loading, error, onReload }: ConfigViewProps) {
  const [modal, setModal] = useState<ModalState>({ kind: 'none' })
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const run = useCallback(
    async (action: () => Promise<unknown>) => {
      setBusy(true)
      setActionError(null)
      try {
        await action()
        await onReload()
        setModal({ kind: 'none' })
      } catch (err) {
        setActionError(describeError(err))
      } finally {
        setBusy(false)
      }
    },
    [onReload],
  )

  // 用 useMemo 固定引用：`config?.providers ?? []` 每次渲染都会新建数组，
  // 会让下游 useMemo 的依赖每帧变化。
  const providers = useMemo(() => config?.providers ?? [], [config])
  const tiers = config?.tiers ?? []
  const activeModelLabel = config?.active_provider
    ? `${config.active_provider} · ${config.active_model || '（未设模型）'}`
    : '未配置'

  const providerNames = useMemo(() => providers.map((item) => item.name), [providers])

  return (
    <section className="view view-page">
      <div className="page-title">配置</div>
      <div className="page-sub">
        Provider、模型与安全策略。改动写入磁盘上的 config.json，立即生效。
      </div>

      {error ? (
        <div className="banner error">
          <span>配置加载失败：{error}</span>
          <span className="spacer" />
          <button type="button" className="link-btn" onClick={() => void onReload()}>
            重试
          </button>
        </div>
      ) : null}

      {actionError ? (
        <div className="banner error">
          <span>{actionError}</span>
          <span className="spacer" />
          <button type="button" className="link-btn" onClick={() => setActionError(null)}>
            关闭
          </button>
        </div>
      ) : null}

      {config?.legacy_user_dir ? (
        <div className="banner">
          检测到历史数据目录 <code>{config.legacy_user_dir}</code>，当前使用{' '}
          <code>{config.user_dir}</code>。旧目录不会被自动迁移，需要保留请手动复制 config.json。
        </div>
      ) : null}

      <div className="big-card">
        <div className="card-head">
          <div className="card-name">Provider 与模型</div>
          <button
            type="button"
            className="btn"
            onClick={() => {
              setActionError(null)
              setModal({ kind: 'add-provider' })
            }}
          >
            + 添加 Provider
          </button>
        </div>
        <div className="card-desc">
          当前生效：<span className="mono">{activeModelLabel}</span>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>名称</th>
              <th>API Base</th>
              <th>默认模型</th>
              <th>Key</th>
              <th>层级</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {providers.map((provider) => (
              <tr key={provider.name}>
                <td>
                  {provider.name}
                  {provider.is_base ? <span className="tag-sel" style={{ marginLeft: 6 }}>base</span> : null}
                </td>
                <td className="mono">{provider.api_base}</td>
                <td className="mono">{provider.default_model}</td>
                <td className="mono">{provider.has_key ? provider.api_key_masked : '(未配置)'}</td>
                <td>{provider.layer === 'project' ? '项目级' : '用户级'}</td>
                <td>
                  <span className="task-acts">
                    {provider.is_base ? null : (
                      <button
                        type="button"
                        className="btn tiny"
                        disabled={busy}
                        onClick={() => void run(() => api.setActiveProvider(provider.name, provider.default_model))}
                      >
                        设为 base
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn tiny"
                      onClick={() => setModal({ kind: 'key', provider })}
                    >
                      {provider.has_key ? '更换 Key' : '设置 Key'}
                    </button>
                    <button
                      type="button"
                      className="btn tiny"
                      onClick={() => setModal({ kind: 'edit-provider', provider })}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      className="btn tiny danger"
                      onClick={() => setModal({ kind: 'delete-provider', provider })}
                    >
                      删除
                    </button>
                  </span>
                </td>
              </tr>
            ))}
            {providers.length === 0 ? (
              <tr>
                <td colSpan={6}>
                  {loading
                    ? '正在加载配置…'
                    : '尚未配置任何 provider。没有 provider 时 Agent 无法执行，请先添加一个。'}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="big-card">
        <div className="card-head">
          <div className="card-name">SmartRouter 四档</div>
          <label className="kv" style={{ gap: 6, alignItems: 'center' }}>
            <span>启用智能路由</span>
            <input
              type="checkbox"
              checked={Boolean(config?.smart_router_enabled)}
              disabled={busy || !config}
              onChange={(event) => void run(() => api.setSmartRouter(event.target.checked))}
            />
          </label>
        </div>
        <div className="card-desc">按任务复杂度自动选择档位模型；未配置的档位回落到当前 base provider。</div>
        <table className="table">
          <thead>
            <tr>
              <th>档位</th>
              <th>Provider</th>
              <th>模型</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {tiers.map((tier) => (
              <tr key={tier.name}>
                <td>{tier.name}</td>
                <td className="mono">{tier.provider || '—'}</td>
                <td className="mono">{tier.model || '—'}</td>
                <td>{tier.configured ? <span className="pill-ok">已配置</span> : '回落 active'}</td>
                <td>
                  <span className="task-acts">
                    <button
                      type="button"
                      className="btn tiny"
                      disabled={providerNames.length === 0}
                      onClick={() => setModal({ kind: 'tier', tier: tier.name })}
                    >
                      设置
                    </button>
                    <button
                      type="button"
                      className="btn tiny"
                      disabled={busy || !tier.configured}
                      onClick={() => void run(() => api.clearTier(tier.name))}
                    >
                      清除
                    </button>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {providerNames.length === 0 ? (
          <div className="hint">先添加 provider，才能配置档位。</div>
        ) : null}
      </div>

      <SkillsCard />

      <div className="big-card">
        <div className="card-name">安全策略</div>
        <div className="card-desc">审批与策略层由服务端强制，客户端无法绕过</div>
        <table className="table">
          <thead>
            <tr>
              <th>项</th>
              <th>行为</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>HITL 审批</td>
              <td>默认 `execute_command` 必审、`write_file` 确认；由会话 WebSocket 推送审批卡</td>
            </tr>
            <tr>
              <td>路径 / 命令策略</td>
              <td>`PathGuard`（含 symlink 与 cwd 越界）与 `CommandGuard` 直接拒绝，不可被审批放行绕过</td>
            </tr>
            <tr>
              <td>访问令牌</td>
              <td>配置后 REST 与 WebSocket 都需要 Bearer Token；`/healthz` 与静态资源免鉴权</td>
            </tr>
            <tr>
              <td>终端通道</td>
              <td>服务端绑定项目根目录启动；需配置 Token 或 Origin，否则端点直接拒绝</td>
            </tr>
            <tr>
              <td>审计</td>
              <td>工具调用、审批、终端命令写入项目 `.routivus/audit.log`（敏感字段脱敏）</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="big-card">
        <div className="card-name">数据目录</div>
        <table className="table">
          <tbody>
            <tr>
              <td>当前</td>
              <td className="mono">{config?.user_dir ?? '—'}</td>
            </tr>
            <tr>
              <td>存放内容</td>
              <td>config.json（provider 与 Key）、projects.json（项目注册表）、workspace.sqlite3（会话/笔记/事件）</td>
            </tr>
            <tr>
              <td>模式</td>
              <td>{config?.desktop ? '桌面端' : '浏览器 / 服务端'}</td>
            </tr>
          </tbody>
        </table>
        {window.routivus ? (
          <div className="hint">
            日志目录可通过应用菜单或 <code>window.routivus.openLogs()</code> 打开。
          </div>
        ) : null}
      </div>

      {modal.kind === 'add-provider' || modal.kind === 'edit-provider' ? (
        <ProviderFormModal
          provider={modal.kind === 'edit-provider' ? modal.provider : undefined}
          busy={busy}
          error={actionError}
          onClose={() => setModal({ kind: 'none' })}
          onSubmit={(payload) =>
            void run(() =>
              modal.kind === 'edit-provider'
                ? api.updateProvider(modal.provider.name, {
                    api_base: payload.api_base,
                    default_model: payload.default_model,
                    display_name: payload.display_name,
                  })
                : api.createProvider(payload),
            )
          }
        />
      ) : null}

      {modal.kind === 'key' ? (
        <KeyModal
          provider={modal.provider}
          busy={busy}
          error={actionError}
          onClose={() => setModal({ kind: 'none' })}
          onSubmit={(apiKey) => void run(() => api.setProviderKey(modal.provider.name, apiKey))}
        />
      ) : null}

      {modal.kind === 'delete-provider' ? (
        <Modal
          title="删除 Provider"
          description={`将移除「${modal.provider.name}」的配置。base provider 不允许直接删除。`}
          onClose={() => setModal({ kind: 'none' })}
          actions={
            <>
              <button type="button" className="btn" onClick={() => setModal({ kind: 'none' })}>
                取消
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={busy}
                onClick={() => void run(() => api.deleteProvider(modal.provider.name))}
              >
                删除
              </button>
            </>
          }
        >
          {actionError ? <div className="banner error">{actionError}</div> : <div />}
        </Modal>
      ) : null}

      {modal.kind === 'tier' ? (
        <TierModal
          tier={modal.tier}
          providers={providers}
          busy={busy}
          error={actionError}
          onClose={() => setModal({ kind: 'none' })}
          onSubmit={(provider, model) => void run(() => api.setTier(modal.tier, provider, model))}
        />
      ) : null}
    </section>
  )
}

function ProviderFormModal({
  provider,
  busy,
  error,
  onClose,
  onSubmit,
}: {
  provider?: ProviderView
  busy: boolean
  error: string | null
  onClose: () => void
  onSubmit: (payload: {
    name: string
    api_base: string
    default_model: string
    display_name?: string | null
    api_key?: string | null
    set_base?: boolean
  }) => void
}) {
  const editing = Boolean(provider)
  const [name, setName] = useState(provider?.name ?? '')
  const [apiBase, setApiBase] = useState(provider?.api_base ?? '')
  const [defaultModel, setDefaultModel] = useState(provider?.default_model ?? '')
  const [displayName, setDisplayName] = useState(provider?.display_name ?? '')
  const [apiKey, setApiKey] = useState('')
  const [setBase, setSetBase] = useState(!editing)

  const canSubmit = name.trim() && apiBase.trim() && defaultModel.trim()

  return (
    <Modal
      title={editing ? `编辑 Provider：${provider?.name}` : '添加 Provider'}
      description={
        editing
          ? '名称不可修改。API Key 请用列表里的「更换 Key」。'
          : 'API Key 会以明文写入 config.json，请勿在共享机器上填入生产 Key。'
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
            onClick={() =>
              onSubmit({
                name: name.trim(),
                api_base: apiBase.trim(),
                default_model: defaultModel.trim(),
                display_name: displayName.trim() || null,
                ...(editing ? {} : { api_key: apiKey.trim() || null, set_base: setBase }),
              })
            }
          >
            {busy ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {error ? <div className="banner error">{error}</div> : null}
      <div className="field">
        <label htmlFor="pv-name">名称（字母 / 数字 / 下划线 / 连字符）</label>
        <input
          id="pv-name"
          value={name}
          disabled={editing}
          onChange={(event) => setName(event.target.value)}
          placeholder="myproxy"
        />
      </div>
      <div className="field">
        <label htmlFor="pv-base">API Base</label>
        <input
          id="pv-base"
          value={apiBase}
          onChange={(event) => setApiBase(event.target.value)}
          placeholder="https://gateway.example.com/v1"
        />
      </div>
      <div className="field">
        <label htmlFor="pv-model">默认模型</label>
        <input
          id="pv-model"
          value={defaultModel}
          onChange={(event) => setDefaultModel(event.target.value)}
          placeholder="deepseek-v4-pro-0813"
        />
      </div>
      <div className="field">
        <label htmlFor="pv-display">显示名（可选）</label>
        <input
          id="pv-display"
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
        />
      </div>
      {editing ? null : (
        <>
          <div className="field">
            <label htmlFor="pv-key">API Key（可选，也可稍后再设）</label>
            <input
              id="pv-key"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder="sk-..."
            />
          </div>
          <label className="kv" style={{ alignItems: 'center', gap: 6 }}>
            <span>设为当前 base provider</span>
            <input type="checkbox" checked={setBase} onChange={(event) => setSetBase(event.target.checked)} />
          </label>
        </>
      )}
    </Modal>
  )
}

function KeyModal({
  provider,
  busy,
  error,
  onClose,
  onSubmit,
}: {
  provider: ProviderView
  busy: boolean
  error: string | null
  onClose: () => void
  onSubmit: (apiKey: string) => void
}) {
  const [apiKey, setApiKey] = useState('')

  return (
    <Modal
      title={`设置 API Key：${provider.name}`}
      description={
        provider.has_key
          ? `当前为 ${provider.api_key_masked}，保存会覆盖旧值。`
          : '当前未配置 Key。'
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
            disabled={busy || !apiKey.trim()}
            onClick={() => onSubmit(apiKey.trim())}
          >
            {busy ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {error ? <div className="banner error">{error}</div> : null}
      <div className="field">
        <label htmlFor="key-input">API Key</label>
        <input
          id="key-input"
          type="password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          placeholder="sk-..."
        />
      </div>
    </Modal>
  )
}

function SkillsCard() {
  const [skills, setSkills] = useState<SkillView[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setSkills(await api.listSkills())
      setError(null)
    } catch (err) {
      setError(describeError(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const toggle = useCallback(async (skill: SkillView) => {
    setBusy(true)
    setError(null)
    try {
      setSkills(await api.setSkillEnabled(skill.name, !skill.enabled))
    } catch (err) {
      setError(describeError(err))
    } finally {
      setBusy(false)
    }
  }, [])

  return (
    <div className="big-card">
      <div className="card-head">
        <div className="card-name">Skill（任务规范）</div>
        <button type="button" className="btn" disabled={loading || busy} onClick={() => void load()}>
          刷新
        </button>
      </div>
      <div className="card-desc">
        只读的任务规范：索引随系统提示注入，正文由模型按需通过 <code>load_skill</code> 加载。
        放到 <code>&lt;用户目录&gt;/skills/&lt;名称&gt;/SKILL.md</code> 或项目{' '}
        <code>.routivus/skills/</code> 下即可被发现；会话内也可用 <code>/skill list</code>。
      </div>
      {error ? <div className="banner error">{error}</div> : null}
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
              <td>{SKILL_SOURCE_LABEL[skill.source] ?? skill.source}</td>
              <td>
                {skill.valid
                  ? skill.enabled
                    ? <span className="pill-ok">已启用</span>
                    : '已禁用'
                  : `无效：${skill.error}`}
              </td>
              <td>
                <button
                  type="button"
                  className="btn tiny"
                  disabled={busy || !skill.valid}
                  onClick={() => void toggle(skill)}
                >
                  {skill.enabled ? '禁用' : '启用'}
                </button>
              </td>
            </tr>
          ))}
          {skills.length === 0 ? (
            <tr>
              <td colSpan={5}>
                {loading ? '正在加载 Skill…' : '没有发现 Skill。'}
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
      <div className="hint">
        这里列出用户级与内置 Skill；项目级 Skill 依赖项目上下文，请在项目会话里用{' '}
        <code>/skill list</code> 查看与启停。
      </div>
    </div>
  )
}

function TierModal({
  tier,
  providers,
  busy,
  error,
  onClose,
  onSubmit,
}: {
  tier: string
  providers: ProviderView[]
  busy: boolean
  error: string | null
  onClose: () => void
  onSubmit: (provider: string, model: string) => void
}) {
  const [provider, setProvider] = useState(providers[0]?.name ?? '')
  const selected = providers.find((item) => item.name === provider)
  const [model, setModel] = useState(selected?.default_model ?? '')

  return (
    <Modal
      title={`配置档位：${tier}`}
      description="模型留空时使用该 provider 的默认模型。"
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={busy || !provider}
            onClick={() => onSubmit(provider, model.trim())}
          >
            {busy ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {error ? <div className="banner error">{error}</div> : null}
      <div className="field">
        <label htmlFor="tier-provider">Provider</label>
        <select
          id="tier-provider"
          className="ask-select"
          value={provider}
          onChange={(event) => {
            setProvider(event.target.value)
            const next = providers.find((item) => item.name === event.target.value)
            setModel(next?.default_model ?? '')
          }}
        >
          {providers.map((item) => (
            <option key={item.name} value={item.name}>
              {item.name}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="tier-model">模型</label>
        <input
          id="tier-model"
          list="tier-model-options"
          value={model}
          onChange={(event) => setModel(event.target.value)}
          placeholder={selected?.default_model ?? ''}
        />
        <datalist id="tier-model-options">
          {(selected?.models ?? []).map((item) => (
            <option key={item} value={item} />
          ))}
        </datalist>
      </div>
    </Modal>
  )
}
