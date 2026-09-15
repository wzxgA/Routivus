import { useCallback, useMemo, useState } from 'react'
import * as api from '../../api'
import type {
  ConfigSnapshot,
  MaxTokensField,
  ModelLimit,
  ProviderView,
  RouterState,
} from '../../api/types'
import { describeError } from '../../state/errors'
import { formatNumber } from '../../utils/format'
import { Modal } from '../common/Modal'

/** 窗口 / 输出上限的取值范围，与后端 provider_service 的校验保持一致。 */
const MIN_CONTEXT_WINDOW = 1024
const MAX_LIMIT = 10_000_000

/**
 * 输出上限字段名三选一：不同网关叫法不一，且无法可靠自动探测，所以交给用户选
 * （见 plans/enhancement/06 §4.6）。空串表示该 provider 干脆不发送。
 */
const OUTPUT_FIELD_OPTIONS = [
  { value: 'max_tokens', label: 'max_tokens（默认，绝大多数网关）' },
  { value: 'max_completion_tokens', label: 'max_completion_tokens（OpenAI 新推理模型）' },
  { value: '', label: '不发送（该网关不支持限制输出长度）' },
]

/** 列表里的能力摘要：窗口 / 输出上限 / 覆盖了几个模型。 */
function limitSummary(provider: ProviderView) {
  const window = provider.context_window ? formatNumber(provider.context_window) : '默认'
  const output = provider.max_output_tokens > 0 ? formatNumber(provider.max_output_tokens) : '不限制'
  const overrides = Object.keys(provider.model_limits ?? {}).length
  return `${window} / ${output}${overrides > 0 ? ` · ${overrides} 个模型覆盖` : ''}`
}

/**
 * 把表单里的覆盖行收敛成请求载荷：只保留填了值的项，空对象＝清空覆盖表
 * （后端整表覆盖语义，见 plans/enhancement/06 §4.1）。
 */
function buildLimits(
  rows: Record<string, { window: string; max_output: string }>,
): Record<string, ModelLimit> {
  const out: Record<string, ModelLimit> = {}
  for (const [model, row] of Object.entries(rows)) {
    const item: ModelLimit = {}
    const window = Number(row.window)
    if (row.window.trim() && Number.isFinite(window) && window > 0) item.window = window
    const maxOutput = Number(row.max_output)
    if (row.max_output.trim() && Number.isFinite(maxOutput) && maxOutput > 0) {
      item.max_output = maxOutput
    }
    if (Object.keys(item).length > 0) out[model] = item
  }
  return out
}

interface ConfigViewProps {
  config: ConfigSnapshot | null
  loading: boolean
  error: string | null
  onReload: () => Promise<ConfigSnapshot | null>
  /** 会话里的路由状态（`App` 已有）：用来在四档表里标出"当前生效"（方案 08 §4.3）。 */
  router?: RouterState | null
  /** 打开智能路由数据看板（方案 13）。导航里没有这页的入口，只从这张卡片进。 */
  onOpenRouterInsights?: () => void
}

type ModalState =
  | { kind: 'none' }
  | { kind: 'add-provider' }
  | { kind: 'edit-provider'; provider: ProviderView }
  | { kind: 'key'; provider: ProviderView }
  | { kind: 'delete-provider'; provider: ProviderView }
  | { kind: 'tier'; tier: string }

export function ConfigView({
  config,
  loading,
  error,
  onReload,
  router = null,
  onOpenRouterInsights,
}: ConfigViewProps) {
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
              <th>窗口 / 输出上限</th>
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
                <td className="mono">{limitSummary(provider)}</td>
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
                <td colSpan={7}>
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
          {onOpenRouterInsights ? (
            <button type="button" className="btn" onClick={onOpenRouterInsights}>
              详情 →
            </button>
          ) : null}
        </div>
        <div className="card-desc">
          按任务复杂度自动选择档位模型；未配置的档位回落到当前 base provider。
          「当前生效」取自会话里最近一轮的路由结果。
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>档位</th>
              <th>Provider</th>
              <th>模型</th>
              <th>状态</th>
              <th>当前生效</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {tiers.map((tier) => {
              const isActive = Boolean(router?.tier) && router?.tier === tier.name
              const resolvedProvider = tier.resolved_provider || ''
              const resolvedModel = tier.resolved_model || ''
              // "实际会用"与"显式配置"不一致 = 会回落：未配置，或该 provider 缺 API Key
              // （两种情况后端都会整档回落到 active）
              const fallsBack =
                Boolean(resolvedProvider || resolvedModel) &&
                (resolvedProvider !== tier.provider || resolvedModel !== tier.model)
              const fallback = fallsBack
                ? `实际 → ${resolvedProvider || '—'} · ${resolvedModel || '—'}`
                : ''
              return (
                <tr key={tier.name}>
                  <td>{tier.name}</td>
                  <td className="mono">{tier.provider || '—'}</td>
                  <td className="mono">{tier.model || '—'}</td>
                  <td>
                    {tier.configured ? (
                      fallsBack ? (
                        <span className="dim">已配置 · 实际回落</span>
                      ) : (
                        <span className="pill-ok">已配置</span>
                      )
                    ) : (
                      '回落 active'
                    )}
                  </td>
                  <td>
                    {isActive ? <span className="pill-ok">本轮生效</span> : null}
                    {fallback ? (
                      <span className="dim mono">
                        {isActive ? ' ' : ''}
                        {fallback}
                      </span>
                    ) : null}
                    {!isActive && !fallback ? '—' : null}
                  </td>
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
              )
            })}
          </tbody>
        </table>
        {providerNames.length === 0 ? (
          <div className="hint">先添加 provider，才能配置档位。</div>
        ) : null}
        {config?.smart_router_enabled && !router?.tier ? (
          <div className="hint">先在会话里发一轮消息，「当前生效」列就会标出本轮用的是哪一档。</div>
        ) : null}
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
                    context_window: payload.context_window,
                    max_output_tokens: payload.max_output_tokens,
                    max_tokens_field: payload.max_tokens_field,
                    model_limits: payload.model_limits,
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
    context_window: number
    max_output_tokens: number
    max_tokens_field: MaxTokensField
    model_limits: Record<string, ModelLimit>
  }) => void
}) {
  const editing = Boolean(provider)
  const [name, setName] = useState(provider?.name ?? '')
  const [apiBase, setApiBase] = useState(provider?.api_base ?? '')
  const [defaultModel, setDefaultModel] = useState(provider?.default_model ?? '')
  const [displayName, setDisplayName] = useState(provider?.display_name ?? '')
  const [apiKey, setApiKey] = useState('')
  const [setBase, setSetBase] = useState(!editing)
  // 能力上限：空字符串表示「该项不覆盖」，与 0（不限制）区分开
  const [contextWindow, setContextWindow] = useState(String(provider?.context_window || 128000))
  const [maxOutput, setMaxOutput] = useState(String(provider?.max_output_tokens ?? 0))
  const [outputField, setOutputField] = useState<MaxTokensField>(
    (provider?.max_tokens_field as MaxTokensField) ?? 'max_tokens',
  )
  const [limits, setLimits] = useState<Record<string, { window: string; max_output: string }>>(
    () => {
      const initial: Record<string, { window: string; max_output: string }> = {}
      for (const [model, limit] of Object.entries(provider?.model_limits ?? {})) {
        initial[model] = {
          window: limit.window ? String(limit.window) : '',
          max_output: limit.max_output ? String(limit.max_output) : '',
        }
      }
      return initial
    },
  )

  // 覆盖区列出 provider 的模型列表 + 已存在覆盖但不在列表里的模型（后者标出来，
  // 免得删了模型之后残留的条目变成看不见的配置）。
  const overrideModels = useMemo(() => {
    const names = new Set<string>(provider?.models ?? [])
    for (const model of Object.keys(provider?.model_limits ?? {})) names.add(model)
    return [...names]
  }, [provider])

  const windowValue = Number(contextWindow)
  const windowValid =
    Number.isInteger(windowValue) &&
    windowValue >= MIN_CONTEXT_WINDOW &&
    windowValue <= MAX_LIMIT
  const maxOutputValue = Number(maxOutput || 0)
  const maxOutputValid =
    Number.isInteger(maxOutputValue) && maxOutputValue >= 0 && maxOutputValue <= MAX_LIMIT

  const canSubmit =
    name.trim() && apiBase.trim() && defaultModel.trim() && windowValid && maxOutputValid

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
                context_window: windowValue,
                max_output_tokens: maxOutputValue,
                max_tokens_field: outputField,
                model_limits: buildLimits(limits),
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
      <div className="field">
        <label htmlFor="pv-window">上下文窗口（token）</label>
        <input
          id="pv-window"
          type="number"
          min={MIN_CONTEXT_WINDOW}
          max={MAX_LIMIT}
          value={contextWindow}
          onChange={(event) => setContextWindow(event.target.value)}
          placeholder="128000"
        />
        <div className="hint">
          该 provider 的默认值，用于上下文预算与界面使用率。同一 provider 下不同模型可以在下面单独覆盖。
        </div>
        {windowValid ? null : (
          <div className="hint" style={{ color: 'var(--danger)' }}>
            上下文窗口必须是 {MIN_CONTEXT_WINDOW} ~ {formatNumber(MAX_LIMIT)} 之间的整数。
          </div>
        )}
      </div>
      <div className="field">
        <label htmlFor="pv-max-output">最大输出（token，0 = 不限制）</label>
        <input
          id="pv-max-output"
          type="number"
          min={0}
          max={MAX_LIMIT}
          value={maxOutput}
          onChange={(event) => setMaxOutput(event.target.value)}
        />
        <div className="hint">
          0 表示不下发输出上限，由服务商决定长度（长回答不会被截断）。填了才会限制，模型可能「说到一半停」。
        </div>
      </div>
      <div className="field">
        <label htmlFor="pv-output-field">输出上限字段</label>
        <select
          id="pv-output-field"
          className="ask-select"
          value={outputField}
          onChange={(event) => setOutputField(event.target.value as MaxTokensField)}
        >
          {OUTPUT_FIELD_OPTIONS.map((option) => (
            <option key={option.value || 'none'} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <div className="hint">
          网关不认该字段时会报 400 并给出提示；按提示换成另一个名字，或选「不发送」。
        </div>
      </div>
      {editing && overrideModels.length > 0 ? (
        <div className="field">
          <label>按模型覆盖（留空 = 用 provider 默认）</label>
          {overrideModels.map((model) => (
            <div className="kv" key={model} style={{ gap: 8, alignItems: 'center' }}>
              <span className="mono" style={{ minWidth: 140 }}>
                {model}
              </span>
              <input
                type="number"
                min={MIN_CONTEXT_WINDOW}
                max={MAX_LIMIT}
                placeholder="窗口"
                value={limits[model]?.window ?? ''}
                onChange={(event) =>
                  setLimits((current) => ({
                    ...current,
                    [model]: { window: event.target.value, max_output: current[model]?.max_output ?? '' },
                  }))
                }
              />
              <input
                type="number"
                min={0}
                max={MAX_LIMIT}
                placeholder="最大输出"
                value={limits[model]?.max_output ?? ''}
                onChange={(event) =>
                  setLimits((current) => ({
                    ...current,
                    [model]: { window: current[model]?.window ?? '', max_output: event.target.value },
                  }))
                }
              />
            </div>
          ))}
          <div className="hint">覆盖表整表保存：清空某一行的两个输入即删除该模型的覆盖。</div>
        </div>
      ) : null}
      {editing || overrideModels.length > 0 ? null : (
        <div className="hint">保存后可在「编辑」里为单个模型覆盖窗口与最大输出。</div>
      )}
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

// Skill 管理已迁至独立页面 frontend/src/components/skills/SkillsView.tsx（路由 #/skills）。

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
