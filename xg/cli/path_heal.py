"""首次运行自愈 PATH：让 `pip install` 的用户新开终端即能直接使用 `xg-cli`。

原理
----
pip 把 ``xg-cli`` 命令可执行文件装进 Python 的 ``scripts`` 目录
（``sysconfig.get_path("scripts")``），但 pip 不会改 PATH。本模块在入口
最前面(首次运行) 探测该目录是否已被加入「持久化 PATH」：
  - Windows：写入注册表用户 PATH（HKCU\\Environment\\Path）
  - macOS / Linux：追加到 ~/.zshrc / ~/.bashrc / ~/.bash_profile（缺一写 .profile）

幂等：已存在则不做任何事；可用 ``XG_AUTO_PATH=0`` 全局关闭。
返回 ``True`` 表示本次执行了新增操作（用于提示用户重开终端）。
"""

from __future__ import annotations

import os
import shutil
import sysconfig

ENV_FLAG = "XG_AUTO_PATH"
_RC_CANDIDATES = (".zshrc", ".bashrc", ".bash_profile")
_DISPATCH_NAMES = ("xg-cli.exe", "xg-cli")


def ensure_on_path() -> bool:
    """尝试把命令 scripts 目录加入持久用户 PATH。

    返回 True 表示本次做了新增；False 表示已就绪 / 已关闭 / 无需处理。
    """
    if _disabled():
        return False
    scripts = _scripts_dir()
    # 目录可能因存储型异常(微软商店 Python)探测错：必须确认里面真有命令文件才写。
    if not scripts or not os.path.isdir(scripts) or not _has_dispatch(scripts):
        return False
    if _persist_contains(scripts):
        return False
    try:
        _write(scripts)
    except Exception:
        return False
    # 让当前进程立即能用（重开终端则走持久 PATH）
    cur = os.environ.get("PATH", "")
    if not _seg_contains(cur, scripts):
        os.environ["PATH"] = scripts + os.pathsep + cur
    if os.name == "nt":
        _notify_win()
    return True


def path_status() -> dict:
    """只读诊断：命令目录、命令文件与当前/持久 PATH 状态（不做任何修改）。"""
    scripts = _scripts_dir()
    command_file = _dispatch_path(scripts) if scripts else ""
    current = os.environ.get("PATH", "")
    return {
        "scripts_dir": scripts,
        "command_file": command_file,
        "in_current_path": bool(scripts) and _seg_contains(current, scripts),
        "in_persisted_path": bool(scripts) and _persist_contains(scripts),
        "auto_path_disabled": _disabled(),
    }


def _disabled() -> bool:
    return os.environ.get(ENV_FLAG, "1").strip().lower() in {"0", "false", "no", "off"}


def _dispatch_path(scripts_dir: str) -> str:
    """在给定目录中返回命令文件绝对路径；不存在返回空串。"""
    for name in _DISPATCH_NAMES:
        candidate = os.path.join(scripts_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return ""


def _has_dispatch(scripts_dir: str) -> bool:
    return bool(_dispatch_path(scripts_dir))


def _which_dispatch() -> str:
    """当前能否直接解析 xg-cli？能就返回其真实路径，否则空串。"""
    found = shutil.which("xg-cli")
    return found or ""


def _record_dispatch() -> str:
    """从包元数据(RECORD)读 pip 实际写入的命令文件路径；取不到返回空串。

    微软商店版 Python 的 sysconfig 会指向只读的 WindowsApps 目录，而 pip 实际把
    可执行文件写进 per-user Scripts；RECORD 记录的是真实写入路径，跨发行版一致。
    """
    try:
        import importlib.metadata as md

        dist = md.distribution("xg-cli")
    except Exception:
        return ""
    for f in dist.files or ():
        base = os.path.basename(f.name.replace("\\", "/"))
        if base in _DISPATCH_NAMES or base.startswith("xg-cli"):
            try:
                resolved = str(dist.locate_file(f.name))
            except Exception:
                continue
            # RECORD 路径可能因损坏/非标准安装指向不存在的文件，只接受真实存在者。
            if resolved and os.path.isfile(resolved):
                return resolved
    return ""


def _scripts_dir() -> str:
    """返回命令真实所在目录（best-effort），解析失败返回空串。

    解析优先级（跨 Python 发行版，避免微软商店版误报只读目录）：
      1. shutil.which：当前已能解析命令 → 直接取父目录；
      2. 包元数据 RECORD：pip 实际写入的命令文件路径的父目录；
      3. sysconfig.get_path("scripts")：最后一击（调用方会再确认命令文件存在）。
    """
    dispatch = _which_dispatch() or _record_dispatch()
    if dispatch:
        return os.path.dirname(dispatch)
    try:
        return sysconfig.get_path("scripts") or ""
    except Exception:
        return ""


def _persist_contains(entry: str) -> bool:
    """判断 entry 是否已在「持久化」用户 PATH 中（以持久配置为准，幂等）。"""
    if os.name == "nt":
        return _win_user_path_contains(entry)
    return _rc_contains(entry)


def _write(entry: str) -> None:
    if os.name == "nt":
        _win_add(entry)
    else:
        _rc_add(entry)


def _seg_contains(path: str, entry: str) -> bool:
    return any(seg == entry for seg in path.split(os.pathsep))


# ---------------- POSIX（macOS / Linux） ----------------

def _home() -> str:
    return os.path.expanduser("~")


def _rc_files() -> list[str]:
    home = _home()
    return [os.path.join(home, c) for c in _RC_CANDIDATES if os.path.isfile(os.path.join(home, c))]


def _rc_contains(entry: str) -> bool:
    for rc in _rc_files():
        try:
            with open(rc, encoding="utf-8", errors="ignore") as f:
                if entry in f.read():
                    return True
        except OSError:
            continue
    return False


def _rc_add(entry: str) -> None:
    files = _rc_files()
    target = files[0] if files else os.path.join(_home(), ".profile")
    line = f'\n# XG-CLI PATH\nexport PATH="{entry}:$PATH"\n'
    with open(target, "a", encoding="utf-8") as f:
        f.write(line)


# ---------------- Windows ----------------

def _win_user_path_contains(entry: str) -> bool:
    try:
        import winreg
    except ImportError:
        return _seg_contains(os.environ.get("PATH", ""), entry)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Path")
    except (FileNotFoundError, OSError):
        return False
    return entry in [p for p in str(value).split(";") if p]


def _win_add(entry: str) -> None:
    import winreg

    # KEY_WRITE 不含 KEY_QUERY_VALUE：打开时必须同时申请读权限，
    # 否则下面读取现有 Path 会抛 PermissionError（WinError 5），写入整次失败。
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
    ) as key:
        try:
            value, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value = ""
        parts = [p for p in str(value).split(";") if p]
        if entry not in parts:
            parts.append(entry)
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(parts))


def _notify_win() -> None:
    """广播 WM_SETTINGCHANGE，提示系统环境变量已变更。"""
    try:
        import ctypes

        hwnd_broadcast = 0xFFFF
        wm_settingchange = 0x001A
        ctypes.windll.user32.SendMessageTimeoutW(
            hwnd_broadcast, wm_settingchange, 0, "Environment", 0x02, 10000, None
        )
    except Exception:
        pass