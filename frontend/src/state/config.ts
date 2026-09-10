import { useCallback, useEffect, useState } from 'react'
import * as api from '../api'
import type { ConfigSnapshot } from '../api/types'
import { describeError } from './errors'

export interface ConfigState {
  config: ConfigSnapshot | null
  loading: boolean
  error: string | null
  reload: () => Promise<ConfigSnapshot | null>
}

/**
 * 配置快照（provider / 四档 / 开关）。
 *
 * 在 App 层只取一次并向下传：配置页需要它做编辑，顶栏模型选择器需要它列模型，
 * 各取一次会造成重复请求与状态不一致。
 */
export function useConfigSnapshot(): ConfigState {
  const [config, setConfig] = useState<ConfigSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const snapshot = await api.getConfig()
      setConfig(snapshot)
      setError(null)
      return snapshot
    } catch (err) {
      setError(describeError(err))
      return null
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  return { config, loading, error, reload }
}
