"""项目 cwd 绑定的终端通道。

两条后端路径：

- ``PtyBackend``（默认）—— pywinpty 提供的 Windows ConPTY 伪终端。是真正的
  终端：ANSI 颜色、resize、交互式程序、Ctrl-C 都正常。pywinpty 是可选依赖
  （``pip install "routivus[terminal]"``）。
- ``OneShotBackend``（需显式 ``ROUTIVUS_TERMINAL_BACKEND=oneshot``）—— 计划
  Phase 5 第 4 条规定的兜底「受限 Command Runner」：每条输入起一个新进程在
  项目根执行。没有 cd 延续性、没有环境变量累积、交互式程序不可用。

**不做管道持久 shell**：Windows 上子进程一旦接管道就转块缓冲（输出要攒够几
KB 才吐），``cmd.exe`` 非交互模式下也不保证输出提示符、没有「命令结束」信号，
且没有 ANSI 与作业控制 —— 严格劣于一次性执行器，却同样带着持久 shell 的逃逸
问题。

**安全边界（重要，README 有同样说明）**：命令黑名单只匹配命令字符串，看不到
shell 的当前目录，因此拦不住持久化 shell 里的 ``cd ..``；Windows 上没有非特权
chroot 类原语。本模块保证的是「进程由服务端绑定项目根启动、客户端无法指定
路径」，**不是**「操作系统阻止进程访问根外资源」。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import queue as _queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from routivus.safety.audit import AuditLogger
from routivus.safety.guards import guard_tool_call
from routivus.server import winproc

logger = logging.getLogger("routivus.server.terminal")

MIN_COLS, MAX_COLS = 20, 400
MIN_ROWS, MAX_ROWS = 5, 200

_READ_SIZE = 4096

# 一次性执行器启动提示（UTF-8 字节，避免在终端里乱码）
_ONESHOT_BANNER = (
    "\x1b[2m[oneshot] 每条命令独立执行，不保留 cd / 环境变量状态。\x1b[0m\r\n"
).encode("utf-8")


class TerminalUnavailableError(RuntimeError):
    """后端不可用（例如 Windows 上缺 pywinpty）。"""


def pty_available() -> bool:
    """pywinpty 是否可用（只有 Windows 才有意义）。"""
    if sys.platform != "win32":
        return False
    try:
        import winpty  # noqa: F401
    except ImportError:
        return False
    return True


def default_shell(configured: str = "") -> str:
    """解析要启动的 shell：显式配置 > %COMSPEC% > cmd.exe。"""
    if configured.strip():
        return configured.strip()
    return os.environ.get("COMSPEC", "") or "cmd.exe"


@dataclass(frozen=True)
class TerminalSpec:
    """一个终端进程的启动参数。``cwd`` 只能来自服务端的项目根。"""

    terminal_id: str
    project_id: str
    cwd: Path
    shell: str
    cols: int
    rows: int
    env: dict[str, str]


def build_env(cwd: Path) -> dict[str, str]:
    """构造子进程环境：把用户目录与临时目录收进项目根内，使 ``~`` 落在根里。"""
    env = dict(os.environ)
    env["HOME"] = str(cwd)
    env["USERPROFILE"] = str(cwd)
    temp = cwd / ".routivus" / "tmp"
    try:
        temp.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - 只读项目目录时退回系统临时目录
        temp = Path(os.environ.get("TEMP", str(cwd)))
    env["TEMP"] = str(temp)
    env["TMP"] = str(temp)
    return env


def new_terminal_id() -> str:
    return f"t-{uuid4().hex[:8]}"


def _decode(raw: bytes) -> str:
    """Windows 中文环境的输出解码：先 utf-8，再 gbk（对齐 tool/builtin.py）。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", "replace")


# --------------------------------------------------------------------------
# 后端
# --------------------------------------------------------------------------


class TerminalBackend(Protocol):
    """终端进程的统一接口。"""

    name: str

    @property
    def pid(self) -> int | None: ...

    @property
    def alive(self) -> bool: ...

    async def start(self) -> None: ...

    async def write(self, data: str) -> None: ...

    def read_blocking(self, stop: threading.Event) -> bytes | None:
        """由专用读线程调用；返回 None 表示读到 EOF 或已停止。"""
        ...

    async def resize(self, cols: int, rows: int) -> None: ...

    async def close(self, *, grace: float = 3.0) -> None: ...


class PtyBackend:
    """Windows ConPTY 伪终端（pywinpty）。"""

    name = "conpty"

    def __init__(self, spec: TerminalSpec) -> None:
        self.spec = spec
        self._proc: Any = None
        # 作业对象句柄：KILL_ON_JOB_CLOSE，关闭句柄即由内核收割整棵进程树
        # （含 shell 派生的孙进程），且不受 pid 复用影响。
        self._job: int | None = None

    @property
    def pid(self) -> int | None:
        return getattr(self._proc, "pid", None)

    @property
    def alive(self) -> bool:
        if self._proc is None:
            return False
        try:
            return bool(self._proc.isalive())
        except Exception:  # pragma: no cover - pywinpty 内部错误
            return False

    async def start(self) -> None:
        try:
            import winpty
        except ImportError as exc:  # pragma: no cover - select_backend 已预先拦截
            raise TerminalUnavailableError("pywinpty 未安装") from exc
        # /Q 关回显，/D 禁用 AutoRun 注册表键（否则机器上配置的命令会在每个
        # 终端里自动执行）。spawn 本身阻塞，放线程里避免卡住事件循环。
        argv = [self.spec.shell, "/Q", "/D"]
        self._proc = await asyncio.to_thread(
            winpty.PtyProcess.spawn,
            argv,
            cwd=str(self.spec.cwd),
            env=self.spec.env,
            dimensions=(self.spec.rows, self.spec.cols),
        )
        # 尽量早地把进程纳入作业。子进程创建与 Assign 之间有一个微秒级窗口，
        # 但此刻 shell 还没执行任何命令、不可能已经派生出孙进程，所以可接受；
        # 残余风险由 TerminalSession._reap 的 taskkill /F /T 兜底。
        self._job = winproc.create_kill_on_close_job()
        winproc.assign_process_to_job(self._job, self.pid or 0)

    async def write(self, data: str) -> None:
        if self._proc is None:
            raise TerminalUnavailableError("终端进程未启动")
        await asyncio.to_thread(self._proc.write, data)

    def read_blocking(self, stop: threading.Event) -> bytes | None:
        proc = self._proc
        if proc is None or stop.is_set():
            return None
        try:
            chunk = proc.read(_READ_SIZE)
        except EOFError:
            return None
        except Exception as exc:  # pragma: no cover - 进程被杀时 pywinpty 会抛
            logger.debug("pty read failed: %s", exc)
            return None
        if not chunk:
            return None
        # pywinpty 的 read() 返回 str（ConPTY 在线路上就是 UTF-8，它已按 utf-8
        # 解码，且会逐字节续读直到能解出来）。统一还原成 bytes 交给上层，
        # 让输出分块与编码判定只有一处实现。
        return chunk.encode("utf-8") if isinstance(chunk, str) else chunk

    async def resize(self, cols: int, rows: int) -> None:
        if self._proc is None:
            return
        try:
            # 注意：pywinpty 的参数顺序是 (rows, cols)，与线协议相反
            self._proc.setwinsize(rows, cols)
        except Exception as exc:  # pragma: no cover - 进程已退出时
            logger.debug("setwinsize failed: %s", exc)

    async def close(self, *, grace: float = 3.0) -> None:
        proc, self._proc = self._proc, None
        job, self._job = self._job, None
        if proc is not None:
            try:
                await asyncio.to_thread(proc.terminate, True)
            except Exception as exc:  # pragma: no cover
                logger.debug("pty terminate failed: %s", exc)
        # 关掉句柄会连带收割作业内所有进程（含孙进程）。
        if job is not None:
            winproc.close_job(job)


class OneShotBackend:
    """受限命令执行器：每条输入一个进程，cwd 固定为项目根。

    不做自动降级 —— 静默把终端换成命令框会让 `cd` / venv 悄悄失效。
    """

    name = "oneshot"

    def __init__(self, spec: TerminalSpec, *, timeout: float = 120.0) -> None:
        self.spec = spec
        self.timeout = timeout
        self._out: "_queue.Queue[bytes]" = _queue.Queue()
        self.exit_code: int | None = None
        self.last_duration_ms: int = 0

    @property
    def pid(self) -> int | None:
        # 没有常驻进程；每次执行的子进程随任务结束，由 subprocess 自身回收。
        return None

    @property
    def alive(self) -> bool:
        return False

    async def start(self) -> None:
        self._out.put(_ONESHOT_BANNER)

    async def write(self, data: str) -> None:
        command = data.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not command or command == "\n":
            return
        started = time.perf_counter()
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                cwd=str(self.spec.cwd),
                env=self.spec.env,
                capture_output=True,
                timeout=self.timeout,
                creationflags=creationflags,
            )
            self.exit_code = completed.returncode
            self._out.put(_decode(completed.stdout).encode("utf-8", "replace"))
            self._out.put(_decode(completed.stderr).encode("utf-8", "replace"))
            if completed.returncode != 0:
                self._out.put(f"\n[exit] {completed.returncode}\n".encode("utf-8"))
        except subprocess.TimeoutExpired:
            self.exit_code = None
            self._out.put(f"\n[timeout] 命令超过 {self.timeout:.0f}s 未结束，已终止\n".encode("utf-8"))
        except OSError as exc:
            self.exit_code = None
            self._out.put(f"\n[error] {exc}\n".encode("utf-8"))
        finally:
            self.last_duration_ms = int((time.perf_counter() - started) * 1000)

    def read_blocking(self, stop: threading.Event) -> bytes | None:
        while not stop.is_set():
            try:
                return self._out.get(timeout=0.1)
            except _queue.Empty:
                continue
        return None

    async def resize(self, cols: int, rows: int) -> None:
        # 一次性执行没有屏幕尺寸的概念。
        return None

    async def close(self, *, grace: float = 3.0) -> None:
        return None


def select_backend(spec: TerminalSpec, mode: str, *, command_timeout: float = 120.0) -> TerminalBackend:
    """按配置选择后端。``auto`` 在缺 pywinpty 时**明确报错**，不静默降级。"""
    if mode == "oneshot":
        return OneShotBackend(spec, timeout=command_timeout)
    if mode == "conpty":
        if not pty_available():
            raise TerminalUnavailableError(
                'ConPTY 后端需要 pywinpty：pip install "routivus[terminal]"'
            )
        return PtyBackend(spec)
    # auto：优先 ConPTY；不可用时给出明确指引，而不是悄悄换成命令框 ——
    # 静默降级会让 cd / venv 失效却看不出来。
    if pty_available():
        return PtyBackend(spec)
    raise TerminalUnavailableError(
        'ConPTY 后端不可用（未安装 pywinpty）。请执行 pip install "routivus[terminal]"；'
        "若接受无 cd、无环境变量延续的受限命令执行器，可设置 "
        "ROUTIVUS_TERMINAL_BACKEND=oneshot。"
    )


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------

SendFn = Callable[[dict[str, Any]], Awaitable[None]]


class TerminalSession:
    """一个终端连接对应的进程 + 输出泵。

    ``send`` 是异步回调而非 WebSocket 对象，这样整条链路（含输出分块、丢块
    计数、进程回收）都能脱离真实连接做单元测试。
    """

    def __init__(
        self,
        *,
        spec: TerminalSpec,
        backend: TerminalBackend,
        send: SendFn,
        audit: AuditLogger | None = None,
        chunk_bytes: int = 8_192,
        flush_interval: float = 0.033,
        queue_max: int = 256,
        max_output_bytes: int = 262_144,
        kill_grace: float = 3.0,
        on_closed: Callable[[str], None] | None = None,
    ) -> None:
        self.spec = spec
        self.backend = backend
        self._send = send
        self._audit = audit
        self.chunk_bytes = max(1, chunk_bytes)
        self.flush_interval = max(0.001, flush_interval)
        self.queue_max = max(1, queue_max)
        self.max_output_bytes = max_output_bytes
        self.kill_grace = kill_grace
        self._on_closed = on_closed

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._pump: asyncio.Task | None = None
        self._pending_get: asyncio.Future | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._eof_seen = False
        self._dropped = 0
        self._seq = 0
        self._total_bytes = 0
        self._closed = False
        self._closed_reason = ""
        self._line_buffer = ""
        self.last_activity = time.monotonic()

    # ---------- 只读状态 ----------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def closed_reason(self) -> str:
        return self._closed_reason

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def sequence(self) -> int:
        return self._seq

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self.queue_max)
        await self.backend.start()
        self._pump = asyncio.create_task(self._pump_output())
        self._reader = threading.Thread(
            target=self._read_loop, name=f"term-{self.spec.terminal_id}", daemon=True
        )
        self._reader.start()

    async def close(self, reason: str = "client_closed", *, exit_code: int | None = None) -> None:
        """统一收口：任何结束路径（断连、取消、心跳超时、客户端主动关）都走这里。

        幂等 —— 可能由 `terminal.close` 触发，也可能是 finally 兜底。
        """
        if self._closed:
            return
        self._closed = True
        self._closed_reason = reason
        # 1) 停读线程
        self._stop.set()
        if self._reader is not None and self._reader.is_alive():
            await asyncio.to_thread(self._reader.join, 1.0)
        # 2) 停输出泵
        if self._pump is not None:
            self._pump.cancel()
            try:
                await self._pump
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover
                logger.debug("output pump ended with error", exc_info=True)
        if self._pending_get is not None and not self._pending_get.done():
            self._pending_get.cancel()
        # 3) 收割进程树
        pid = self.backend.pid
        await self.backend.close(grace=self.kill_grace)
        if pid:
            await self._reap(pid)
        if self._on_closed is not None:
            try:
                self._on_closed(self.spec.terminal_id)
            except Exception:  # pragma: no cover
                logger.debug("on_closed callback failed", exc_info=True)
        try:
            await self._send(
                {
                    "type": "terminal.closed",
                    "terminal_id": self.spec.terminal_id,
                    "reason": reason,
                    "exit_code": exit_code,
                }
            )
        except Exception:  # pragma: no cover - 连接通常已经断了
            logger.debug("failed to deliver terminal.closed", exc_info=True)

    async def _reap(self, pid: int) -> None:
        """taskkill /F /T 兜底。

        主路径是 PtyBackend 里作业对象的 KILL_ON_JOB_CLOSE，但它可能没绑上
        （进程已处于一个不允许嵌套的作业中），而 taskkill 单独用又有 pid 复用
        竞态 —— 两条都走。
        """
        try:
            await asyncio.to_thread(winproc.kill_process_tree, pid, grace=self.kill_grace)
        except Exception:  # pragma: no cover
            logger.debug("kill_process_tree failed pid=%s", pid, exc_info=True)

    # ---------- 输入 ----------

    async def write(self, data: str) -> tuple[bool, str]:
        """处理一次客户端输入。返回 (是否已转发, 拒绝原因)。"""
        if self._closed:
            return False, "terminal_closed"
        self.last_activity = time.monotonic()
        lines = self._take_lines(data)
        for line in lines:
            result = guard_tool_call(
                self.spec.cwd, "execute_command", {"command": line, "cwd": str(self.spec.cwd)}
            )
            if not result.ok:
                if self._audit is not None:
                    self._audit.blocked(
                        result.reason,
                        origin="terminal",
                        command=line,
                        detail=result.detail,
                        terminal_id=self.spec.terminal_id,
                    )
                return False, "command_blocked"
        started = time.perf_counter()
        await self.backend.write(data)
        duration_ms = int((time.perf_counter() - started) * 1000)
        if self._audit is not None:
            for line in lines:
                self._audit.terminal_command(
                    line,
                    cwd=str(self.spec.cwd),
                    ok=True,
                    duration_ms=duration_ms,
                    exit_code=getattr(self.backend, "exit_code", None),
                    terminal_id=self.spec.terminal_id,
                )
        return True, ""

    def _take_lines(self, data: str) -> list[str]:
        """取出本次输入中「已回车」的整行，用于策略校验与审计。

        真实终端的按键是逐个到达的（`r`、`m`、空格 …… `\\r`），所以只有到回车
        那一刻才知道用户敲的是什么。此时命令还没执行 —— 拒绝就是不转发这个
        回车，已敲入的文本停在输入行里不执行。粘贴整条命令的情况则在转发前
        就拦下，命令不会出现在终端里。
        """
        if "\r" not in data and "\n" not in data:
            self._line_buffer += data
            return []
        normalized = data.replace("\r\n", "\n").replace("\r", "\n")
        parts = normalized.split("\n")
        if parts and parts[-1] == "":
            parts = parts[:-1]
        lines: list[str] = []
        for index, part in enumerate(parts):
            line = (self._line_buffer + part) if index == 0 else part
            if line.strip():
                lines.append(line.strip())
        self._line_buffer = ""
        return lines

    async def resize(self, cols: int, rows: int) -> tuple[int, int]:
        clamped = (min(MAX_COLS, max(MIN_COLS, cols)), min(MAX_ROWS, max(MIN_ROWS, rows)))
        await self.backend.resize(*clamped)
        self.last_activity = time.monotonic()
        return clamped

    # ---------- 输出 ----------

    def _read_loop(self) -> None:
        """专用读线程。

        不能用 `asyncio.to_thread`：常驻的阻塞读会永久占用共享默认线程池里的
        一个 worker，几个终端就能饿死进程里所有其他 to_thread 调用。
        """
        while not self._stop.is_set():
            chunk = self.backend.read_blocking(self._stop)
            if chunk is None:
                break
            loop = self._loop
            if loop is None:
                break
            try:
                loop.call_soon_threadsafe(self._on_chunk, chunk)
            except RuntimeError:  # pragma: no cover - 事件循环已关闭
                return
        loop = self._loop
        if loop is not None and not self._stop.is_set():
            try:
                loop.call_soon_threadsafe(self._mark_eof)
            except RuntimeError:  # pragma: no cover
                pass

    def _on_chunk(self, chunk: bytes) -> None:
        """事件循环线程内调用：入队；满了就丢块并计数。"""
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            # 绝不阻塞读线程：管道缓冲写满会连子进程一起卡住。这里用「可见的
            # 数据丢失」换「子进程不停摆」。
            self._dropped += 1

    def _mark_eof(self) -> None:
        self._eof_seen = True

    async def _pump_output(self) -> None:
        """输出泵：分块、刷写、丢块上报，最后发出 terminal.exit。

        刻意不用 `asyncio.wait_for(queue.get(), ...)` 做定时刷写 —— wait_for
        超时会取消内层 get()，若此刻元素刚好被投递就会永久丢失。这里改成
        「保留一个不取消的 pending get + asyncio.wait 超时」，超时时只是刷写
        已有缓冲，等待继续。
        """
        loop = asyncio.get_running_loop()
        buffer = bytearray()
        last_flush = loop.time()
        while True:
            # 已经就绪的等待先收掉，避免下面判 EOF 时漏掉刚投递进来的最后一块。
            if self._pending_get is not None and self._pending_get.done():
                try:
                    buffer.extend(self._pending_get.result())
                except asyncio.CancelledError:  # pragma: no cover
                    pass
                self._pending_get = None

            if self._eof_seen and self._queue.empty():
                if self._pending_get is not None:
                    self._pending_get.cancel()
                    self._pending_get = None
                if not await self._flush(buffer):
                    return
                await self._send_dropped()
                await self._safe_send({"type": "terminal.exit", "terminal_id": self.spec.terminal_id})
                return

            if self._pending_get is None:
                self._pending_get = asyncio.ensure_future(self._queue.get())
            done, _ = await asyncio.wait({self._pending_get}, timeout=self.flush_interval)
            if done:
                item = self._pending_get.result()
                self._pending_get = None
                buffer.extend(item)
            now = loop.time()
            if buffer and (len(buffer) >= self.chunk_bytes or now - last_flush >= self.flush_interval):
                if not await self._flush(buffer):
                    return
                last_flush = now
            if self._dropped:
                await self._send_dropped()

    async def _flush(self, buffer: bytearray) -> bool:
        """发送缓冲内容。返回 False 表示触到输出上限、输出泵应结束。

        必须在这里按 chunk_bytes 切片：单次 read 可能一次性拿到远大于
        chunk_bytes 的数据（例如 `dir /s` 的一整屏），只靠入队粒度切分会让
        单个 terminal.output 事件超出约定大小。
        """
        if not buffer:
            return True
        data = bytes(buffer)
        buffer.clear()
        for start in range(0, len(data), self.chunk_bytes):
            piece = data[start: start + self.chunk_bytes]
            if self._total_bytes + len(piece) > self.max_output_bytes:
                allowed = max(0, self.max_output_bytes - self._total_bytes)
                if allowed:
                    await self._send_output(piece[:allowed])
                await self._safe_send(
                    {
                        "type": "error",
                        "terminal_id": self.spec.terminal_id,
                        "code": "output_limit_reached",
                        "message": "终端输出超过上限，连接已关闭",
                    }
                )
                asyncio.create_task(self.close("limit"))
                return False
            await self._send_output(piece)
        return True

    async def _send_output(self, chunk: bytes) -> None:
        self._seq += 1
        self._total_bytes += len(chunk)
        self.last_activity = time.monotonic()
        try:
            text = chunk.decode("utf-8")
            encoding = "utf8"
        except UnicodeDecodeError:
            # 中文 Windows 会持续吐 GBK；跨块做有损解码会静默损坏数据，
            # 用 base64 如实透传。
            text = base64.b64encode(chunk).decode("ascii")
            encoding = "base64"
        await self._safe_send(
            {
                "type": "terminal.output",
                "terminal_id": self.spec.terminal_id,
                "seq": self._seq,
                "data": text,
                "encoding": encoding,
            }
        )

    async def _send_dropped(self) -> None:
        count, self._dropped = self._dropped, 0
        if not count:
            return
        await self._safe_send(
            {"type": "terminal.output.dropped", "terminal_id": self.spec.terminal_id, "count": count}
        )

    async def _safe_send(self, payload: dict[str, Any]) -> None:
        try:
            await self._send(payload)
        except Exception:  # pragma: no cover - 连接断开后不应影响回收流程
            logger.debug("terminal send failed", exc_info=True)
