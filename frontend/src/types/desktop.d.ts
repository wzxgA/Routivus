/** Electron preload 暴露的原生能力（浏览器环境下为 undefined）。 */
export interface RoutivusDesktopBridge {
  /** 弹出系统目录选择器；取消返回 null */
  pickDirectory: () => Promise<string | null>
  /** 在资源管理器中打开日志目录 */
  openLogs: () => Promise<boolean>
  appInfo: () => Promise<{
    version: string
    platform: string
    logDir: string
    staticDir: string
  }>
}

declare global {
  interface Window {
    routivus?: RoutivusDesktopBridge
  }
}
