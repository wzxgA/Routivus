import { createContext, useContext } from 'react'
import type { Project } from '../api/types'
import type { ThemeName } from '../theme'

export interface WorkspaceValue {
  projects: Project[]
  loading: boolean
  error: string | null
  refresh: () => Promise<void>
  theme: ThemeName
  toggleTheme: () => void
}

// 与 Provider 分文件存放：workspace.tsx 只导出组件，保持 Fast Refresh 干净。
export const WorkspaceContext = createContext<WorkspaceValue | null>(null)

export function useWorkspace(): WorkspaceValue {
  const value = useContext(WorkspaceContext)
  if (!value) throw new Error('useWorkspace 必须在 WorkspaceProvider 内使用')
  return value
}
