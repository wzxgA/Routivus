import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import * as api from '../api'
import { ApiError } from '../api/client'
import type { Project } from '../api/types'
import { applyTheme, otherTheme, readStoredTheme, type ThemeName } from '../theme'

interface WorkspaceValue {
  projects: Project[]
  loading: boolean
  error: string | null
  refresh: () => Promise<void>
  theme: ThemeName
  toggleTheme: () => void
}

const WorkspaceContext = createContext<WorkspaceValue | null>(null)

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.isNetworkError) return error.message
    return `${error.message}（${error.code}）`
  }
  if (error instanceof Error) return error.message
  return '未知错误'
}

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

export function useWorkspace(): WorkspaceValue {
  const value = useContext(WorkspaceContext)
  if (!value) throw new Error('useWorkspace 必须在 WorkspaceProvider 内使用')
  return value
}
