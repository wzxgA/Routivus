import { createRoot } from 'react-dom/client'
import { App } from './App'
import { WorkspaceProvider } from './state/workspace'
import './styles/app.css'
import { applyTheme, readStoredTheme } from './theme'

applyTheme(readStoredTheme())

const container = document.getElementById('root')
if (!container) throw new Error('找不到 #root 挂载点')

// 不启用 StrictMode：它会在开发环境重放副作用，导致会话自动创建与
// WebSocket 连接被重复触发，干扰真实的幂等 / 重连行为验证。
createRoot(container).render(
  <WorkspaceProvider>
    <App />
  </WorkspaceProvider>,
)
