import type { RouterState, TierView } from '../../api/types'
import { describeNotes } from '../../utils/routerNotes'

interface RouterPopoverProps {
  router: RouterState
  tiers: TierView[]
}

/**
 * 顶栏「⚡ 档位」chip 的浮层（方案 08 §4.3）：
 * 四档全局视图（当前生效高亮）+ 本轮判定依据 + 实际使用的模型 + 耗时。
 *
 * 只读：所有数据都来自 `router.updated` / 快照与配置快照，不在这里发请求。
 */
export function RouterPopover({ router, tiers }: RouterPopoverProps) {
  const notes = describeNotes(router.notes)
  const activeTier = router.tier ?? ''
  const elapsed = typeof router.elapsed_ms === 'number' ? `${router.elapsed_ms} ms` : ''
  const load = router.load_ms ? `（含加载 ${router.load_ms} ms）` : ''

  return (
    <div className="router-pop">
      <div className="router-pop-head">
        <span className="router-pop-title">本轮路由</span>
        <span className="router-pop-meta">{elapsed ? `${elapsed}${load}` : ''}</span>
      </div>

      {router.error ? <div className="router-pop-error">{router.error}</div> : null}

      <table className="router-tiers">
        <thead>
          <tr>
            <th>档位</th>
            <th>模型</th>
            <th>配置</th>
          </tr>
        </thead>
        <tbody>
          {tiers.map((tier) => {
            const isActive = Boolean(activeTier) && tier.name === activeTier
            const resolvedProvider = tier.resolved_provider || ''
            const resolvedModel = tier.resolved_model || ''
            // 回落 = "实际会用"与"显式配置"不一致（未配置，或该 provider 缺 API Key）
            const fallsBack =
              Boolean(resolvedProvider || resolvedModel) &&
              (resolvedProvider !== tier.provider || resolvedModel !== tier.model)
            const provider = tier.provider || resolvedProvider
            const model = tier.model || resolvedModel
            const label = fallsBack ? (tier.configured ? '已配置 · 回落' : '回落 active') : '已配置'
            return (
              <tr key={tier.name} className={isActive ? 'active' : ''}>
                <td>
                  {tier.name}
                  {isActive ? <span className="router-tier-mark"> ●</span> : null}
                </td>
                <td className="mono">
                  {model || '—'}
                  {provider ? <span className="dim"> · {provider}</span> : null}
                  {fallsBack && resolvedModel && resolvedModel !== model ? (
                    <span className="dim"> → {resolvedModel}</span>
                  ) : null}
                </td>
                <td>{label}</td>
              </tr>
            )
          })}
          {tiers.length === 0 ? (
            <tr>
              <td colSpan={3}>正在读取档位配置…</td>
            </tr>
          ) : null}
        </tbody>
      </table>

      <div className="router-pop-section">
        <div className="router-pop-title">为什么是 {activeTier || '—'}</div>
        {notes.length === 0 ? (
          <div className="hint">本轮没有可解释的记录（后端为旧版本，只下发档位）。</div>
        ) : (
          <ul className="router-notes">
            {notes.map((note) => (
              <li key={note.raw} className={note.tone === 'muted' ? 'muted' : undefined}>
                {note.label}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="router-pop-foot">
        {router.switched
          ? `本轮实际使用 ${router.provider || '—'} · ${router.model || '—'}（已切换）`
          : router.model
            ? `本轮沿用当前模型（目标 ${router.provider || '—'} · ${router.model}）`
            : '本轮未换模型'}
      </div>
    </div>
  )
}
