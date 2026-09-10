import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import readline from 'node:readline'
import type { Logger } from './log'

/** 与 routivus/server/__main__.py 的 READY_PREFIX 保持一致 */
const READY_PREFIX = 'ROUTIVUS_DESKTOP_READY '
const HANDSHAKE_TIMEOUT_MS = 30_000
const HEALTH_TIMEOUT_MS = 20_000
const GRACEFUL_STOP_MS = 8_000

export interface ServerHandle {
  readonly port: number
  readonly baseUrl: string
  readonly child: ChildProcess
}

export interface LaunchOptions {
  /** Python 解释器路径 */
  pythonPath: string
  /** 作为子进程 cwd；打包后是 resources 目录，源码运行时是仓库根 */
  workingDir: string
  /** 前端构建产物目录，交给 FastAPI 同源托管 */
  staticDir: string
  logger: Logger
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * 解析 Python 解释器。
 *
 * 优先级：显式环境变量 → 打包内置运行时 → PATH。开发期用 PATH 上的解释器，
 * 打包后用 resources/python 里的 python-build-standalone 运行时。
 */
export function resolvePython(resourcesDir: string, packaged: boolean): string {
  const explicit = process.env.ROUTIVUS_PYTHON?.trim()
  if (explicit) return explicit

  if (packaged) {
    const candidates = [
      path.join(resourcesDir, 'python', 'python.exe'),
      path.join(resourcesDir, 'python', 'bin', 'python3'),
    ]
    for (const candidate of candidates) {
      if (existsSync(candidate)) return candidate
    }
  }

  return process.platform === 'win32' ? 'python' : 'python3'
}

function waitForHandshake(
  child: ChildProcess,
  logger: Logger,
): Promise<{ port: number; staticDirEnabled: boolean }> {
  return new Promise((resolve, reject) => {
    let settled = false
    const stderrTail: string[] = []
    const stdoutTail: string[] = []

    const finish = (fn: () => void): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      fn()
    }

    const timer = setTimeout(() => {
      finish(() =>
        reject(
          new Error(
            `后端在 ${HANDSHAKE_TIMEOUT_MS / 1000}s 内未就绪。\n` +
              `--- stdout ---\n${stdoutTail.join('')}\n--- stderr ---\n${stderrTail.join('')}`,
          ),
        ),
      )
    }, HANDSHAKE_TIMEOUT_MS)

    child.stdout?.on('data', (chunk: Buffer) => {
      const text = chunk.toString('utf8')
      if (stdoutTail.join('').length < 8000) stdoutTail.push(text)
    })

    const rl = readline.createInterface({ input: child.stdout!, crlfDelay: Infinity })
    rl.on('line', (line) => {
      logger.info(`[server] ${line}`)
      if (!line.startsWith(READY_PREFIX)) return
      try {
        const payload = JSON.parse(line.slice(READY_PREFIX.length)) as {
          port?: number
          error?: string
          detail?: string
          static?: boolean
        }
        if (payload.error) {
          finish(() => reject(new Error(`后端启动失败（${payload.error}）：${payload.detail ?? ''}`)))
          return
        }
        if (typeof payload.port !== 'number' || payload.port <= 0) {
          finish(() => reject(new Error(`握手缺少可用端口：${line}`)))
          return
        }
        finish(() => resolve({ port: payload.port as number, staticDirEnabled: Boolean(payload.static) }))
      } catch (err) {
        finish(() => reject(new Error(`握手行解析失败：${line} / ${(err as Error).message}`)))
      }
    })

    child.stderr?.on('data', (chunk: Buffer) => {
      const text = chunk.toString('utf8')
      if (stderrTail.join('').length < 8000) stderrTail.push(text)
      for (const line of text.split(/\r?\n/)) {
        if (line.trim()) logger.error(`[server] ${line}`)
      }
    })

    child.once('exit', (code, signal) => {
      finish(() =>
        reject(
          new Error(
            `后端进程提前退出（code=${code ?? 'null'} signal=${signal ?? 'null'}）。\n` +
              `--- stdout ---\n${stdoutTail.join('')}\n--- stderr ---\n${stderrTail.join('')}`,
          ),
        ),
      )
    })

    child.once('error', (err) => {
      finish(() => reject(new Error(`无法启动 Python 进程：${err.message}`)))
    })
  })
}

async function waitForHealthy(baseUrl: string, logger: Logger): Promise<void> {
  const deadline = Date.now() + HEALTH_TIMEOUT_MS
  let lastError = ''
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseUrl}/healthz`)
      if (response.ok) {
        logger.info(`健康检查通过：${baseUrl}/healthz`)
        return
      }
      lastError = `HTTP ${response.status}`
    } catch (err) {
      lastError = (err as Error).message
    }
    await delay(250)
  }
  throw new Error(`后端端口已监听但健康检查未通过（${lastError}）`)
}

export async function startServer(options: LaunchOptions): Promise<ServerHandle> {
  const { pythonPath, workingDir, staticDir, logger } = options
  logger.info(`启动后端：${pythonPath} -m routivus.server --desktop (cwd=${workingDir})`)

  const child = spawn(pythonPath, ['-m', 'routivus.server', '--desktop'], {
    cwd: workingDir,
    env: {
      ...process.env,
      ROUTIVUS_DESKTOP: '1',
      // 0 = 由操作系统分配空闲端口，避免与用户机器上别的东西撞端口
      ROUTIVUS_SERVER_PORT: '0',
      ROUTIVUS_STATIC_DIR: staticDir,
    },
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  })

  const { port, staticDirEnabled } = await waitForHandshake(child, logger)
  if (!staticDirEnabled) {
    logger.error(`警告：后端未启用前端静态托管，请检查 ROUTIVUS_STATIC_DIR=${staticDir}`)
  }
  const baseUrl = `http://127.0.0.1:${port}`
  await waitForHealthy(baseUrl, logger)

  return { port, baseUrl, child }
}

async function forceKillTree(pid: number, logger: Logger): Promise<void> {
  if (process.platform !== 'win32') {
    try {
      process.kill(pid, 'SIGKILL')
    } catch {
      /* 已经退出了 */
    }
    return
  }
  logger.info(`优雅退出超时，强制回收进程树 pid=${pid}`)
  await new Promise<void>((resolve) => {
    const killer = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true })
    killer.once('close', () => resolve())
    killer.once('error', () => resolve())
  })
}

/**
 * 停止后端。
 *
 * 优先关 stdin 触发 uvicorn 优雅停机（lifespan 会回收终端进程树），超时再
 * taskkill /T /F 兜底——Windows 上 Node 的 kill() 是强杀，不做这一步会留下
 * 孤儿 shell。
 */
export async function stopServer(handle: ServerHandle, logger: Logger): Promise<void> {
  const { child } = handle
  if (child.exitCode !== null || child.signalCode !== null) return
  const pid = child.pid
  logger.info('正在停止后端…')

  const closed = new Promise<void>((resolve) => child.once('close', () => resolve()))
  try {
    child.stdin?.end()
  } catch {
    /* 管道可能已断 */
  }

  const graceful = await Promise.race([
    closed.then(() => true),
    delay(GRACEFUL_STOP_MS).then(() => false),
  ])

  if (graceful) {
    logger.info('后端已优雅退出')
    return
  }
  if (pid !== undefined) await forceKillTree(pid, logger)
  await Promise.race([closed, delay(3000)])
}
