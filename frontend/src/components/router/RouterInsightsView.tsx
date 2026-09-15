import { useCallback, useEffect, useState } from 'react'
import * as api from '../../api'
import type { RouterInsights } from '../../api/types'
import { describeError } from '../../state/errors'
import { DiagnosisBanner } from './DiagnosisBanner'
import { EvolutionPanel } from './EvolutionPanel'
import { LearningPanel } from './LearningPanel'
import { RuntimePanel } from './RuntimePanel'
import { StatusCards } from './StatusCards'

const RANGES = [7, 30, 90]

/**
 * 智能路由数据看板（方案 13）。
 *
 * 数据全部来自一个只读聚合接口，页面只负责渲染 + 切换时间范围 + 手动刷新：
 * - **结论与建议由后端生成**：门槛常量与原因码都在后端，前端复刻必然漂移；
 * - **不做实时推送**：路由数据是分钟/天级变化的，推送通道留给会话事件；
 * - **空态也要有信息量**：现实是开局大部分数字为 0（首次进化通常在第二到四周），
 *   所以每一块的空态都在解释"为什么还没动、还差什么"。
 */
export function RouterInsightsView() {
  const [days, setDays] = useState(30)
  const [data, setData] = useState<RouterInsights | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async (span: number) => {
    setLoading(true)
    try {
      setData(await api.fetchRouterInsights(span))
      setError(null)
    } catch (err) {
      setError(describeError(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(days)
  }, [days, load])

  return (
    <section className="view view-page">
      <div className="page-title">智能路由数据</div>
      <div className="page-sub">
        产物来源与原因码、每轮判定、校准与自学习规则、样本积累与本地进化。数据来自本机的自适应
        目录与已落库的路由事件，不联网。
      </div>

      <div className="ri-toolbar">
        {RANGES.map((span) => (
          <button
            type="button"
            key={span}
            className={`btn${span === days ? ' primary' : ''}`}
            onClick={() => setDays(span)}
          >
            近 {span} 天
          </button>
        ))}
        <span className="ri-toolbar-spacer" />
        <span className="ri-stamp">
          {data ? `更新于 ${data.generated_at.replace('T', ' ').slice(0, 19)}（${data.timezone}）` : '正在读取…'}
        </span>
        <button type="button" className="btn" disabled={loading} onClick={() => void load(days)}>
          {loading ? '刷新中…' : '刷新'}
        </button>
      </div>

      {error ? <div className="banner error">读取失败：{error}</div> : null}

      {data === null ? (
        <div className="hint">正在读取路由数据…</div>
      ) : (
        <>
          <DiagnosisBanner diagnosis={data.diagnosis} />
          <StatusCards status={data.status} />
          <RuntimePanel runtime={data.runtime} days={data.days} />
          <LearningPanel
            calibration={data.calibration}
            rules={data.rules}
            samples={data.samples}
            limits={data.limits}
          />
          <EvolutionPanel evolution={data.evolution} limits={data.limits} />
        </>
      )}
    </section>
  )
}
