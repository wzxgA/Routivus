import { createWriteStream, mkdirSync, type WriteStream } from 'node:fs'
import path from 'node:path'

export interface Logger {
  info(message: string): void
  error(message: string): void
  /** 日志目录，供「打开日志」按钮使用 */
  readonly dir: string
  readonly file: string
}

/**
 * 极简文件日志。
 *
 * 桌面端的失败大多发生在"窗口还没出来"之前（Python 起不来、依赖缺失、
 * 端口被占），此时 UI 无从展示——所以启动链路必须落盘，用户报障时能直接
 * 拿到 desktop.log。
 */
export function createLogger(userDataDir: string): Logger {
  const dir = path.join(userDataDir, 'logs')
  mkdirSync(dir, { recursive: true })
  const file = path.join(dir, 'desktop.log')
  const stream: WriteStream = createWriteStream(file, { flags: 'a' })

  const write = (level: 'info' | 'error', message: string): void => {
    const line = `[${new Date().toISOString()}] [${level}] ${message}\n`
    stream.write(line)
    // 开发时同时打到终端，方便 `npm start` 直接观察
    if (!process.stdout.destroyed) process.stdout.write(line)
  }

  return {
    info: (message) => write('info', message),
    error: (message) => write('error', message),
    dir,
    file,
  }
}
