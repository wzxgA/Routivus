import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'
import { writeFileSync } from 'node:fs'
import path from 'node:path'
import { createLogger, type Logger } from './log'
import { resolvePython, startServer, stopServer, type ServerHandle } from './python'

const APP_NAME = 'Routivus'

let logger: Logger | null = null
let server: ServerHandle | null = null
let mainWindow: BrowserWindow | null = null
let shuttingDown = false

interface Layout {
  /** 打包后是 resources 目录，源码运行时是仓库根目录 */
  workingDir: string
  /** 前端构建产物目录（交给 FastAPI 同源托管） */
  staticDir: string
}

function resolveLayout(): Layout {
  if (app.isPackaged) {
    return {
      workingDir: process.resourcesPath,
      staticDir: path.join(process.resourcesPath, 'app-ui'),
    }
  }
  // 源码运行：electron . 的 appPath 是 desktop/，其父目录就是仓库根
  const repoRoot = path.resolve(app.getAppPath(), '..')
  return {
    workingDir: repoRoot,
    staticDir: path.join(repoRoot, 'frontend', 'dist'),
  }
}

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1360,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: APP_NAME,
    backgroundColor: '#faf9f5',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  win.once('ready-to-show', () => win.show())
  // 外链一律交给系统浏览器，不在应用窗口里打开
  win.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url)
    return { action: 'deny' }
  })
  return win
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * 启动失败时的可见化页面。
 *
 * 桌面端最难受的失败模式是"白屏"——用户完全不知道发生了什么。这里把原始
 * 错误、日志路径、以及"打开日志目录"按钮一并渲染出来，让用户能直接报障。
 */
function showFatalError(detail: string): void {
  const logDir = logger?.dir ?? app.getPath('userData')
  const html = [
    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
    `<title>${APP_NAME} 启动失败</title>`,
    '<style>',
    'body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;',
    'background:#faf9f5;color:#2c2925;margin:0;padding:28px 32px;line-height:1.6}',
    'h1{font-size:17px;margin:0 0 6px}',
    'p{font-size:12.5px;color:#8d887d;margin:0 0 14px}',
    'pre{background:#fff;border:1px solid #e7e3d8;border-radius:8px;padding:12px 14px;',
    'font-family:Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-all;',
    'max-height:320px;overflow:auto;margin:0 0 14px}',
    'code{font-family:Consolas,monospace}',
    'button{font-size:12.5px;padding:6px 14px;border:1px solid #e7e3d8;border-radius:7px;',
    'background:#fff;cursor:pointer}',
    'button:hover{border-color:#b0473a;color:#b0473a}',
    '</style></head><body>',
    '<h1>后端启动失败</h1>',
    '<p>Routivus 的本地服务没能起来，下面是原始错误。</p>',
    `<pre>${escapeHtml(detail)}</pre>`,
    `<p>日志目录：<code>${escapeHtml(logDir)}</code></p>`,
    '<button onclick="window.routivus && window.routivus.openLogs()">打开日志目录</button>',
    '</body></html>',
  ].join('')

  const errorFile = path.join(app.getPath('userData'), 'startup-error.html')
  try {
    writeFileSync(errorFile, html, 'utf8')
  } catch (err) {
    dialog.showErrorBox(`${APP_NAME} 启动失败`, `${detail}\n\n(错误页写入失败：${String(err)})`)
    return
  }

  const win = createWindow()
  mainWindow = win
  void win.loadFile(errorFile)
}

function registerIpc(layout: Layout): void {
  ipcMain.handle('routivus:app-info', () => ({
    version: app.getVersion(),
    platform: process.platform,
    logDir: logger?.dir ?? '',
    staticDir: layout.staticDir,
  }))

  ipcMain.handle('routivus:open-logs', async () => {
    if (!logger) return false
    const failure = await shell.openPath(logger.dir)
    return failure === ''
  })

  // 决策 1a：桌面端注册项目走原生目录选择器，用户点选即授权，
  // 不需要手输路径（动态白名单的接入在 P1 完成）。
  ipcMain.handle('routivus:pick-directory', async () => {
    const result = await dialog.showOpenDialog({
      title: '选择项目根目录',
      properties: ['openDirectory', 'createDirectory'],
    })
    if (result.canceled || result.filePaths.length === 0) return null
    return result.filePaths[0]
  })
}

async function bootstrap(): Promise<void> {
  const layout = resolveLayout()
  logger = createLogger(app.getPath('userData'))
  logger.info(`Routivus desktop 启动：version=${app.getVersion()} packaged=${app.isPackaged}`)
  logger.info(`workingDir=${layout.workingDir}`)
  logger.info(`staticDir=${layout.staticDir}`)

  const pythonPath = resolvePython(layout.workingDir, app.isPackaged)
  logger.info(`python=${pythonPath}`)

  try {
    server = await startServer({
      pythonPath,
      workingDir: layout.workingDir,
      staticDir: layout.staticDir,
      logger,
    })
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    logger.error(`后端启动失败：${message}`)
    showFatalError(message)
    return
  }

  registerIpc(layout)

  const win = createWindow()
  mainWindow = win
  const baseUrl = server.baseUrl
  // 只允许停留在本地源内，其余导航交给系统浏览器
  win.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(baseUrl)) {
      event.preventDefault()
      void shell.openExternal(url)
    }
  })
  win.on('closed', () => {
    mainWindow = null
  })

  await win.loadURL(baseUrl)
  logger.info(`窗口已加载 ${baseUrl}`)
}

const gotSingleInstanceLock = app.requestSingleInstanceLock()

if (!gotSingleInstanceLock) {
  // 已有实例在跑：直接退出，由第一个实例负责聚焦窗口
  app.quit()
} else {
  app.setAppUserModelId('com.routivus.console')

  app.on('second-instance', () => {
    if (!mainWindow) return
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.focus()
  })

  app.on('window-all-closed', () => {
    app.quit()
  })

  app.on('before-quit', (event) => {
    if (shuttingDown || !server) return
    // 先拦住退出，把后端收干净（优雅停机 → 进程树强杀兜底）再真正退出
    event.preventDefault()
    shuttingDown = true
    const handle = server
    const activeLogger = logger
    server = null

    void (async () => {
      if (activeLogger) await stopServer(handle, activeLogger)
      app.quit()
    })()
  })

  void app.whenReady().then(bootstrap)
}
