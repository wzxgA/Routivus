"""项目工作区文件的浏览与编辑（只读列举 + 受控写入）。

完整设计与取舍见 plans/enhancement/04-workspace-files.md。这里的核心约束：

- **路径**：一律 resolve 后校验落在项目根内，挡 `..` 与软链接逃逸——与
  `safety/guards.py` 同一标准，只是入口不同（REST 而非工具调用）
- **忽略目录**：读复用 `tool/builtin.py` 的 `IGNORED_DIRS`；写额外一律拒绝
  （往 node_modules / dist / .git 里写几乎总是误操作）
- **三类护栏**：二进制不下发；有损解码（`lossy`）与截断（`truncated`）的文件
  **拒绝保存**——否则一次保存就把坏字节或半截内容写实了
- **冲突**：内容 sha256 当版本号，不匹配就报冲突，绝不静默覆盖
- **写入**：原子写（临时文件 + `os.replace`）、保留原换行符、落审计

不引 FastAPI：本模块是纯函数 + 领域异常，由 `server/app.py` 负责翻译成 HTTP。
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from routivus.safety.audit import AuditLogger
from routivus.tool.builtin import IGNORED_DIRS

# ---------- 阈值（已定值，见方案 §4.6）----------

MAX_PREVIEW_BYTES = 1 * 1024 * 1024  # 文本预览 / 可编辑上限
MAX_WRITE_BYTES = 5 * 1024 * 1024  # 写入请求体上限
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 图片上限
MAX_LIST_ENTRIES = 500  # 单层目录条目上限（与工具层一致）
BINARY_SNIFF_BYTES = 8192  # 二进制嗅探窗口

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
# 图片 magic bytes：后缀可以撒谎，字节不会
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # webp 的完整签名是 RIFF....WEBP，下面再补一次检查
    (b"BM", "image/bmp"),
)
_TEXT_MEDIA_TYPE = "text/plain; charset=utf-8"


class WorkspaceFileError(Exception):
    """工作区文件操作的领域异常，由 app 层翻译成 HTTP 响应。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# ---------- 路径 ----------


def normalize_rel(raw: str | None) -> str:
    """归一化相对路径：统一分隔符、去掉首尾斜杠与 `.` 段。空串代表项目根。"""
    text = str(raw or "").strip().replace("\\", "/")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise WorkspaceFileError(422, "invalid_path", "路径不能包含 ..")
        parts.append(part)
    return "/".join(parts)


def resolve_in_root(root: Path, raw: str | None) -> Path:
    """把相对路径解析成根内绝对路径；越界（含软链接逃逸）一律拒绝。

    `..` 由 `normalize_rel` 直接拦掉；真正的难点是**软链接**——项目里放一个指向
    `~/.ssh` 的软链就能绕过朴素的前缀比较，所以这里必须 `resolve()` 之后再比对。
    """
    rel = normalize_rel(raw)
    base = root.resolve()
    if Path(str(raw or "")).is_absolute() or str(raw or "").startswith(("/", "\\")):
        raise WorkspaceFileError(422, "invalid_path", "只接受项目内相对路径")
    target = (base / rel).resolve() if rel else base
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise WorkspaceFileError(422, "path_outside_root", f"路径越出项目根：{rel or '.'}") from exc
    return target


def rel_path(root: Path, target: Path) -> str:
    """根内绝对路径 → POSIX 相对路径（根自身返回空串；越界退回文件名，仅用于展示）。"""
    resolved = target.resolve()
    base = root.resolve()
    if resolved == base:
        return ""
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return target.name


def _ignored_anywhere(rel: str) -> bool:
    return any(part in IGNORED_DIRS for part in rel.split("/") if part)


def _require_writable(rel: str) -> None:
    if _ignored_anywhere(rel):
        raise WorkspaceFileError(
            422, "path_ignored", f"不允许写入被忽略的目录（{rel}）"
        )


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


# ---------- 列举 ----------


def _is_link(path: Path) -> bool:
    """软链接或 Windows junction。

    junction 也是"指向别处"的目录，`is_symlink()` 对它返回 False（Python 3.12 起可用
    `os.path.isjunction` 判定），所以两者都要认。
    """
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is None:
        return False
    try:
        return bool(is_junction(path))
    except OSError:
        return False


def _entry(root: Path, child: Path, *, include_ignored: bool) -> dict[str, Any] | None:
    ignored = child.name in IGNORED_DIRS
    if ignored and not include_ignored:
        return None
    symlink = _is_link(child)
    broken = False
    outside = False
    size = 0
    mtime = 0.0
    try:
        stat = child.stat()  # 跟随软链接
        size = stat.st_size
        mtime = stat.st_mtime
    except OSError:
        broken = True
    if symlink:
        try:
            child.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            # 指向根外的软链/junction：列出来但标记出来，点开时会被路径校验拦住
            outside = True
    if child.is_dir():
        kind = "dir"
    elif child.is_file():
        kind = "file"
    else:
        kind = "other"
    return {
        "name": child.name,
        "path": rel_path(root, child),
        "type": kind,
        "size": size,
        "mtime": _iso(mtime) if mtime else "",
        "symlink": symlink,
        "broken": broken,
        "outside": outside,
        "ignored": ignored,
    }


def list_entries(
    root: Path, raw: str | None = None, *, include_ignored: bool = False
) -> dict[str, Any]:
    """列一层目录（逐层懒加载，不做递归扫描）。"""
    target = resolve_in_root(root, raw)
    if not target.exists():
        raise WorkspaceFileError(404, "path_not_found", "目录不存在")
    if not target.is_dir():
        raise WorkspaceFileError(422, "not_a_directory", "目标不是目录")

    rel = rel_path(root, target)
    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        # 目录在前、名字排序（与工具层 list_dir 一致）
        children = sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
    except OSError as exc:
        raise WorkspaceFileError(403, "path_unreadable", f"目录不可读：{exc}") from exc
    for child in children:
        item = _entry(root, child, include_ignored=include_ignored)
        if item is None:
            continue
        if len(entries) >= MAX_LIST_ENTRIES:
            truncated = True
            break
        entries.append(item)
    parent = ""
    if rel:
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return {
        "path": rel,
        "parent": parent,
        "entries": entries,
        "truncated": truncated,
        "limit": MAX_LIST_ENTRIES,
    }


# ---------- 读取 ----------


def _looks_binary(data: bytes) -> bool:
    """二进制嗅探：只看 NUL 字节。

    解码失败但无 NUL 的文件（混了别的编码）不算二进制——那种交给 `lossy`：照样展示，
    但禁止保存（见 `read_text_file`）。
    """
    return b"\x00" in data[:BINARY_SNIFF_BYTES]


def _decode(data: bytes) -> tuple[str, bool]:
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), True


def _detect_line_ending(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "crlf"
    return "lf"


def _version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def read_text_file(root: Path, raw: str | None) -> dict[str, Any]:
    """读文本文件。二进制只给元信息；超限截断并标记（截断/有损均禁止保存）。"""
    target = resolve_in_root(root, raw)
    rel = rel_path(root, target)
    if not target.exists():
        raise WorkspaceFileError(404, "file_not_found", "文件不存在")
    if target.is_dir():
        raise WorkspaceFileError(422, "is_a_directory", "目标是目录")
    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            data = handle.read(MAX_PREVIEW_BYTES + 1)
    except OSError as exc:
        raise WorkspaceFileError(403, "file_unreadable", f"文件不可读：{exc}") from exc

    truncated = size > MAX_PREVIEW_BYTES or len(data) > MAX_PREVIEW_BYTES
    if truncated:
        data = data[:MAX_PREVIEW_BYTES]
    binary = _looks_binary(data)
    if binary:
        return {
            "path": rel,
            "size": size,
            "mtime": _iso(target.stat().st_mtime),
            "version": "",
            "binary": True,
            "lossy": False,
            "truncated": truncated,
            "line_ending": "",
            "content": "",
            "editable": False,
            "reason": "二进制文件，不支持预览",
        }

    text, lossy = _decode(data)
    editable = not truncated and not lossy
    reason = ""
    if truncated:
        reason = f"文件超过 {MAX_PREVIEW_BYTES // 1024}KB，只显示开头部分，不能保存"
    elif lossy:
        reason = "文件包含无法按 UTF-8 解码的字节，为避免写坏，禁止保存"
    return {
        "path": rel,
        "size": size,
        "mtime": _iso(target.stat().st_mtime),
        # 只有完整读到的内容才有资格做版本比对；截断文件本来也不允许保存
        "version": "" if truncated else _version(data),
        "binary": False,
        "lossy": lossy,
        "truncated": truncated,
        "line_ending": _detect_line_ending(text),
        "content": text,
        "editable": editable,
        "reason": reason,
    }


def read_image(root: Path, raw: str | None) -> tuple[bytes, str]:
    """读图片原始字节（给 `<img>` 用）。后缀 + magic bytes 双重校验，避免沦为任意下载器。"""
    target = resolve_in_root(root, raw)
    if not target.is_file():
        raise WorkspaceFileError(404, "file_not_found", "文件不存在")
    if target.suffix.lower() not in IMAGE_SUFFIXES:
        raise WorkspaceFileError(422, "not_an_image", "只支持图片预览")
    size = target.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise WorkspaceFileError(413, "image_too_large", "图片超过 5MB")
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise WorkspaceFileError(403, "file_unreadable", f"文件不可读：{exc}") from exc
    return data, _image_media_type(target, data)


def _image_media_type(target: Path, data: bytes) -> str:
    suffix = target.suffix.lower()
    if suffix == ".svg":
        return "image/svg+xml"
    for magic, media_type in _IMAGE_MAGIC:
        if data.startswith(magic):
            if magic == b"RIFF":
                if data[8:12] == b"WEBP":
                    return media_type
                break
            return media_type
    raise WorkspaceFileError(422, "not_an_image", "文件内容不是可识别的图片格式")


# ---------- 写入 ----------


def _audit(root: Path, rel: str, payload: bytes, *, created: bool, forced: bool) -> None:
    """写入落审计：`force` 覆盖是"明知有外部修改仍覆盖"的唯一痕迹。"""
    AuditLogger(root / ".routivus" / "audit.log", session_id="ui-files").record(
        "file_write", path=rel, bytes=len(payload), created=created, forced=forced
    )


def write_text_file(
    root: Path,
    raw: str | None,
    content: str,
    *,
    expected_version: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """保存文件：原子写 + 版本冲突检测 + 换行符保留。

    - 文件已存在时**必须有 `expected_version`**，否则要求先加载当前内容（409）
    - 内容按 UTF-8 编码；保留原文件的换行符（浏览器会把它规范化成 LF）
    - 父目录必须已存在（与 `write_file` 工具的语义一致）
    """
    rel = normalize_rel(raw)
    if not rel:
        raise WorkspaceFileError(422, "invalid_path", "缺少文件路径")
    target = resolve_in_root(root, rel)
    _require_writable(rel)

    if target.is_dir():
        raise WorkspaceFileError(422, "is_a_directory", "目标是目录")
    if not target.parent.is_dir():
        raise WorkspaceFileError(422, "parent_missing", "父目录不存在")

    exists = target.exists()
    line_ending = "lf"
    current_version = ""
    if exists:
        if target.stat().st_size > MAX_PREVIEW_BYTES:
            # 读接口对这类文件已标记为不可编辑（truncated），这里再兜一层：
            # 避免为了算哈希把几百 MB 读进内存
            raise WorkspaceFileError(
                413, "content_too_large", "文件超过可编辑上限，无法保存"
            )
        try:
            current = target.read_bytes()
        except OSError as exc:
            raise WorkspaceFileError(403, "file_unreadable", f"文件不可读：{exc}") from exc
        current_text, lossy = _decode(current)
        if _looks_binary(current):
            raise WorkspaceFileError(422, "binary_not_editable", "二进制文件不能保存")
        if lossy:
            raise WorkspaceFileError(
                422, "lossy_not_editable", "文件含无法解码的字节，拒绝保存以免写坏"
            )
        line_ending = _detect_line_ending(current_text)
        current_version = _version(current)
        if not force:
            if not expected_version:
                raise WorkspaceFileError(
                    409,
                    "file_conflict",
                    "文件已存在，请先加载当前内容再保存",
                )
            if expected_version != current_version:
                raise WorkspaceFileError(
                    409,
                    "file_conflict",
                    "文件已被外部修改，请选择覆盖或重新加载",
                )

    payload_text = _apply_line_ending(content, line_ending)
    payload = payload_text.encode("utf-8")
    if len(payload) > MAX_WRITE_BYTES:
        raise WorkspaceFileError(413, "content_too_large", "内容超过 5MB 上限")

    _atomic_write(target, payload)
    _audit(root, rel, payload, created=not exists, forced=force)
    return {
        "path": rel,
        "size": len(payload),
        "created": not exists,
        "forced": force,
        "line_ending": line_ending,
        "version": _version(payload),
    }


def _apply_line_ending(content: str, line_ending: str) -> str:
    """按原文件的换行符还原内容。

    浏览器 `<textarea>` 的 value 会把 CRLF 规范化成 LF，不还原的话，在 Windows 上
    改一行会让整个文件的 diff 全变（每行都算改动）。
    """
    if line_ending != "crlf":
        return content
    return content.replace("\r\n", "\n").replace("\n", "\r\n")


def _atomic_write(target: Path, payload: bytes) -> None:
    """先写同目录临时文件再替换：避免"写一半崩了"把原文件截断。"""
    temp = target.parent / f".{target.name}.routivus-{uuid4().hex[:8]}.tmp"
    try:
        temp.write_bytes(payload)
        os.replace(temp, target)
    except OSError as exc:
        raise WorkspaceFileError(403, "write_failed", f"写入失败：{exc}") from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def create_entry(root: Path, raw: str | None, kind: str) -> dict[str, Any]:
    """新建文件或目录。父目录必须已存在（与工具层写入语义一致）。"""
    rel = normalize_rel(raw)
    if not rel:
        raise WorkspaceFileError(422, "invalid_path", "缺少路径")
    if kind not in ("file", "dir"):
        raise WorkspaceFileError(422, "invalid_kind", "kind 只能是 file 或 dir")
    target = resolve_in_root(root, rel)
    _require_writable(rel)
    if target.exists():
        raise WorkspaceFileError(409, "already_exists", "同名文件或目录已存在")
    if not target.parent.is_dir():
        raise WorkspaceFileError(422, "parent_missing", "父目录不存在")
    try:
        if kind == "dir":
            target.mkdir()
        else:
            _atomic_write(target, b"")
    except OSError as exc:
        raise WorkspaceFileError(403, "write_failed", f"创建失败：{exc}") from exc
    if kind == "file":
        _audit(root, rel, b"", created=True, forced=False)
    return {"path": rel, "type": kind, "created": True}
