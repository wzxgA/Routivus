"""项目记忆文件加载与 /init 草稿生成。"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from routivus.llm.client import LlmClient
from routivus.llm.types import Message
from routivus.memory.models import SharedSection
from routivus.safety.audit import redact_text


PROJECT_MEMORY_FILES = ("Routivus.md", "Routivus.local.md")
PROJECT_MEMORY_SYSTEM = (
    "你正在为 Routivus 生成项目记忆文件。只根据提供的项目快照写事实性、可执行的说明，"
    "不要编造不存在的命令、依赖或目录；不要输出密钥或其他敏感值。"
)


@dataclass(frozen=True)
class ProjectMemorySnapshot:
    sections: tuple[SharedSection, ...] = ()
    warnings: tuple[str, ...] = ()
    signatures: tuple[tuple[str, int, int] | tuple[str, None], ...] = ()


def _signature(path: Path) -> tuple[str, int, int] | tuple[str, None]:
    try:
        stat = path.stat()
    except OSError:
        return (str(path), None)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _truncate_lines(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit < 80:
        return text[:limit], True
    left_limit = max(1, (limit - 32) // 2)
    right_limit = max(1, limit - left_limit - 32)
    left = text[:left_limit].rsplit("\n", 1)[0]
    right = text[-right_limit:].split("\n", 1)[-1]
    clipped = f"{left}\n\n... [Routivus memory truncated] ...\n\n{right}"
    return clipped[:limit], True


class ProjectMemoryLoader:
    def __init__(self, project_root: str | Path, max_chars: int = 32_000) -> None:
        self.project_root = Path(project_root).resolve()
        self.max_chars = max(256, int(max_chars))
        self._snapshot: ProjectMemorySnapshot | None = None

    def load(self, force: bool = False) -> ProjectMemorySnapshot:
        paths = [self.project_root / name for name in PROJECT_MEMORY_FILES]
        signatures = tuple(_signature(path) for path in paths)
        if not force and self._snapshot is not None and self._snapshot.signatures == signatures:
            return self._snapshot

        sections: list[SharedSection] = []
        warnings: list[str] = []
        for name, path in zip(PROJECT_MEMORY_FILES, paths):
            if not path.is_file():
                continue
            try:
                with path.open("r", encoding="utf-8-sig") as handle:
                    raw = handle.read(self.max_chars + 1)
            except UnicodeError:
                warnings.append(f"{name} 不是有效 UTF-8，已跳过")
                continue
            except OSError as exc:
                warnings.append(f"读取 {name} 失败：{exc}")
                continue
            raw, truncated = _truncate_lines(raw, self.max_chars)
            safe = redact_text(raw)
            if truncated:
                warnings.append(f"{name} 超过 {self.max_chars} 字符，已按行截断")
            sections.append(SharedSection(source=name, text=safe))

        self._snapshot = ProjectMemorySnapshot(
            sections=tuple(sections),
            warnings=tuple(warnings),
            signatures=signatures,
        )
        return self._snapshot


def build_project_snapshot(project_root: str | Path, max_chars: int = 32_000) -> str:
    """构造 /init 使用的有限、只读项目快照。"""
    root = Path(project_root).resolve()
    excluded = {".git", ".routivus", ".venv", "__pycache__", "node_modules"}
    names: list[str] = []
    try:
        for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if child.name in excluded or child.name.startswith("."):
                continue
            names.append(child.name + ("/" if child.is_dir() else ""))
            if len(names) >= 200:
                break
    except OSError:
        names = []

    parts = [f"项目根目录：{root}", "\n顶层目录：", "\n".join(names) or "（无法读取）"]
    candidates = (
        "README.md",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "Makefile",
    )
    remaining = max_chars - len("\n".join(parts))
    for name in candidates:
        if remaining <= 0:
            break
        path = root / name
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                content = handle.read(min(8_000, remaining) + 1)
        except (OSError, UnicodeError):
            continue
        content, _ = _truncate_lines(content, min(8_000, remaining))
        content = redact_text(content)
        part = f"\n\n--- {name} ---\n{content}"
        parts.append(part)
        remaining -= len(part)
    return "\n".join(parts)[:max_chars]


async def generate_project_memory(
    llm: LlmClient, project_root: str | Path, max_chars: int = 32_000
) -> str:
    target = Path(project_root).resolve() / "Routivus.md"
    if target.exists():
        raise FileExistsError("Routivus.md 已存在，不覆盖已有项目记忆")
    messages = [
        Message(role="system", content=PROJECT_MEMORY_SYSTEM),
        Message(
            role="user",
            content=(
                "请生成一个 Markdown 项目记忆草稿，必须包含：项目概述、技术栈、目录结构、"
                "常用命令、代码约定、验证要求、禁止事项。只输出 Markdown 正文。\n\n"
                + build_project_snapshot(project_root, max_chars)
            ),
        ),
    ]
    parts: list[str] = []
    async for event in llm.stream_chat(messages, tools=None):
        if event.kind == "content" and event.text:
            parts.append(event.text)
    draft = redact_text("".join(parts).strip())
    if not draft:
        raise ValueError("LLM 未生成有效的 Routivus.md 草稿")
    if not draft.startswith("#"):
        draft = "# Routivus Project Memory\n\n" + draft
    return draft


def write_project_memory(project_root: str | Path, content: str) -> Path:
    """以不覆盖已有文件为目标写入 Routivus.md。"""
    root = Path(project_root).resolve()
    target = root / "Routivus.md"
    if target.exists():
        raise FileExistsError("Routivus.md 已存在，不覆盖已有项目记忆")
    root.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".Routivus.md.", suffix=".tmp", dir=root)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content.rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            raise FileExistsError("Routivus.md 在写入期间已被创建，不覆盖已有文件")
        os.replace(temp_path, target)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return target
