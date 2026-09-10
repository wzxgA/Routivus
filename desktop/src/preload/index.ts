import { contextBridge, ipcRenderer } from 'electron'

/**
 * 暴露给渲染进程的最小能力集合。
 *
 * 渲染进程就是前端本体（由 Python 同源托管），默认按普通网页运行；
 * 只有需要"原生能力"时才通过这些通道走主进程——目录选择、打开日志、
 * 读取版本信息。绝不暴露 fs / child_process 这类通用能力。
 */
const desktopApi = {
  /** 弹出系统目录选择器；取消返回 null */
  pickDirectory: (): Promise<string | null> => ipcRenderer.invoke('routivus:pick-directory'),
  /** 在资源管理器中打开日志目录 */
  openLogs: (): Promise<boolean> => ipcRenderer.invoke('routivus:open-logs'),
  /** 版本与路径信息（用于「关于」与排障） */
  appInfo: (): Promise<{
    version: string
    platform: string
    logDir: string
    staticDir: string
  }> => ipcRenderer.invoke('routivus:app-info'),
}

contextBridge.exposeInMainWorld('routivus', desktopApi)

export type RoutivusDesktopApi = typeof desktopApi
