"""Windows 进程树回收原语。

终端通道需要「连接断开后整棵树都能被回收」的保证。单靠 pid 杀进程有两个
问题：shell 派生的子进程（cmd -> python -> node）不会被级联杀掉；pid 还会
被复用，迟到的 taskkill 可能误伤无关进程。

因此这里用 Windows Job Object + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`：句柄一
关，作业内所有进程（含孙进程）由内核接管清理，且不受 pid 复用影响。`taskkill`
作为兜底 —— 进程若已处于无法脱离的作业中，AssignProcessToJobObject 会失败，
这条路径仍需覆盖。

非 Windows 平台全部 no-op，保证模块在任何平台都能导入与单测。
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import time
from ctypes import wintypes

logger = logging.getLogger("routivus.server.winproc")

IS_WINDOWS = sys.platform == "win32"

# --- Win32 常量 ---
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x0000_2000
_JobObjectExtendedLimitInformation = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_SYNCHRONIZE = 0x0010_0000
_WAIT_TIMEOUT = 0x0000_0102
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_STILL_ACTIVE = 259


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():  # type: ignore[no-untyped-def]
    return ctypes.WinDLL("kernel32", use_last_error=True)


def create_kill_on_close_job() -> int | None:
    """建一个「句柄关闭即杀掉全部成员」的作业对象，返回句柄（int）。"""
    if not IS_WINDOWS:
        return None
    try:
        kernel32 = _kernel32()
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.warning("CreateJobObjectW failed error=%s", ctypes.get_last_error())
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            wintypes.HANDLE(job),
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            logger.warning("SetInformationJobObject failed error=%s", ctypes.get_last_error())
            close_job(job)
            return None
        return int(job)
    except (OSError, AttributeError) as exc:
        logger.warning("job object unavailable: %s", exc)
        return None


def assign_process_to_job(job: int | None, pid: int) -> bool:
    """把进程加入作业。

    Windows 8+ 支持嵌套作业；若外层作业不允许嵌套（例如进程已处于一个
    设置了 JOB_OBJECT_LIMIT_BREAKAWAY_OK 之外的作业里），这里会失败并返回
    False —— 调用方需靠 kill_process_tree 兜底。
    """
    if not IS_WINDOWS or job is None or not pid:
        return False
    handle = None
    try:
        kernel32 = _kernel32()
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not handle:
            logger.debug("OpenProcess failed pid=%s error=%s", pid, ctypes.get_last_error())
            return False
        ok = kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(handle))
        if not ok:
            logger.debug("AssignProcessToJobObject failed pid=%s error=%s", pid, ctypes.get_last_error())
            return False
        return True
    except (OSError, AttributeError) as exc:
        logger.debug("assign to job failed pid=%s: %s", pid, exc)
        return False
    finally:
        if handle:
            try:
                _kernel32().CloseHandle(wintypes.HANDLE(handle))
            except (OSError, AttributeError):
                pass


def close_job(job: int | None) -> None:
    """关闭作业句柄；KILL_ON_JOB_CLOSE 会在这一步收割整棵树。"""
    if not IS_WINDOWS or job is None:
        return
    try:
        _kernel32().CloseHandle(wintypes.HANDLE(job))
    except (OSError, AttributeError) as exc:
        logger.debug("CloseHandle(job) failed: %s", exc)


def process_alive(pid: int) -> bool:
    """进程是否仍存活。拿不到句柄（进程已退出 / 无权限）按不存活处理。"""
    if not pid:
        return False
    if not IS_WINDOWS:
        try:
            import os

            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    handle = None
    try:
        kernel32 = _kernel32()
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(_SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        return kernel32.WaitForSingleObject(wintypes.HANDLE(handle), 0) == _WAIT_TIMEOUT
    except (OSError, AttributeError):
        return False
    finally:
        if handle:
            try:
                _kernel32().CloseHandle(wintypes.HANDLE(handle))
            except (OSError, AttributeError):
                pass


def kill_process_tree(pid: int, *, grace: float = 3.0) -> None:
    """兜底收割：taskkill /F /T 结束整棵进程树。

    只在作业对象路径不可用时才真正起作用，但两条路都走一遍是刻意的 ——
    作业句柄可能压根没绑上，而 taskkill 单独使用又有 pid 复用竞态。
    """
    if not pid:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(int(pid))],
            capture_output=True,
            timeout=max(1.0, grace),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("taskkill failed pid=%s: %s", pid, exc)


def wait_for_exit(pid: int, *, timeout: float) -> bool:
    """限时轮询等待进程退出；返回是否在超时前退出。"""
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.05)
    return not process_alive(pid)
