import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import * as api from '../api'
import type { Project } from '../api/types'
import { applyTheme, otherTheme, readStoredTheme, type ThemeName } from '../theme'
import { describeError } from './errors'
import { WorkspaceContext, type WorkspaceValue } from './workspaceContext'

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [projects, setProjects] = useState<Project[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [theme, setTheme] = useState<ThemeName>(() => readStoredTheme())

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const result = await api.listProjects()
      setProjects(result)
      setError(null)
    } catch (err) {
      setError(describeError(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  const toggleTheme = useCallback(() => {
    setTheme((current) => otherTheme(current))
  }, [])

  const value = useMemo<WorkspaceValue>(
    () => ({ projects, loading, error, refresh, theme, toggleTheme }),
    [projects, loading, error, refresh, theme, toggleTheme],
  )

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>
}
