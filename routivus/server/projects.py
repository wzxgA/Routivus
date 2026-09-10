"""Persistent project registry with workspace-bound path validation."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4


class ProjectRegistryError(Exception):
    """Base error for project registry failures."""


class ProjectAlreadyExistsError(ProjectRegistryError):
    """The project root is already registered."""


class ProjectNotFoundError(ProjectRegistryError):
    """The requested project id does not exist."""


class UnsafeProjectPathError(ProjectRegistryError):
    """The project path is invalid or outside configured workspaces."""


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    name: str
    root_path: str
    branch: str | None
    status: str
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


class ProjectRegistry:
    """Store project metadata in a small atomically-written JSON file.

    This is deliberately a registry only: create/update/delete never mutate
    files inside a registered project directory.
    """

    VERSION = 1

    def __init__(
        self,
        storage_path: str | Path,
        allowed_roots: tuple[str | Path, ...] | list[str | Path],
    ) -> None:
        self.storage_path = Path(storage_path).expanduser()
        self.allowed_roots = tuple(self._resolve_allowed_root(root) for root in allowed_roots)
        if not self.allowed_roots:
            raise ValueError("至少需要配置一个允许的工作区根目录")
        self._lock = RLock()

    @staticmethod
    def _resolve_allowed_root(raw: str | Path) -> Path:
        root = Path(raw).expanduser()
        try:
            root = root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"工作区根目录不可用：{raw}") from exc
        if not root.is_dir():
            raise ValueError(f"工作区根目录不是目录：{root}")
        return root

    @staticmethod
    def _validate_name(name: str) -> str:
        value = name.strip()
        if not value:
            raise ValueError("项目名称不能为空")
        if len(value) > 100:
            raise ValueError("项目名称不能超过 100 个字符")
        return value

    def _resolve_project_path(self, raw: str | Path) -> Path:
        candidate = Path(raw).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise UnsafeProjectPathError("项目目录不存在或无法解析") from exc
        if not resolved.is_dir():
            raise UnsafeProjectPathError("项目路径必须是目录")

        for allowed in self.allowed_roots:
            try:
                resolved.relative_to(allowed)
            except ValueError:
                continue
            return resolved
        raise UnsafeProjectPathError("项目目录不在允许的工作区范围内")

    def allow_root(self, raw: str | Path) -> Path:
        """把一个目录追加为允许的工作区根（**仅本次运行有效**）。

        桌面端用它接原生目录选择器：用户在系统对话框里亲手选中的目录即等于
        显式授权。刻意不持久化——每次新增项目都要重新点选，避免"授权一次、
        永久放宽"这种静默扩大权限的行为。
        """
        root = self._resolve_allowed_root(raw)
        with self._lock:
            if not any(_same_path(root, existing) for existing in self.allowed_roots):
                self.allowed_roots = (*self.allowed_roots, root)
        return root

    def allowed_root_strings(self) -> list[str]:
        return [str(root) for root in self.allowed_roots]

    def _read(self) -> list[ProjectRecord]:
        if not self.storage_path.is_file():
            return []
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectRegistryError("项目注册表损坏或无法读取") from exc
        if not isinstance(raw, dict) or raw.get("version") != self.VERSION:
            raise ProjectRegistryError("项目注册表版本不受支持")
        records = raw.get("projects", [])
        if not isinstance(records, list):
            raise ProjectRegistryError("项目注册表格式无效")
        try:
            return [ProjectRecord(**item) for item in records]
        except (TypeError, ValueError) as exc:
            raise ProjectRegistryError("项目注册表记录格式无效") from exc

    def _write(self, records: list[ProjectRecord]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "projects": [asdict(record) for record in records],
        }
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.storage_path.name}.", suffix=".tmp", dir=self.storage_path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.storage_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def list(self) -> list[ProjectRecord]:
        with self._lock:
            return sorted(self._read(), key=lambda record: record.updated_at, reverse=True)

    def get(self, project_id: str) -> ProjectRecord:
        with self._lock:
            for record in self._read():
                if record.id == project_id:
                    return record
        raise ProjectNotFoundError("项目不存在")

    def create(self, name: str, root_path: str | Path) -> ProjectRecord:
        clean_name = self._validate_name(name)
        resolved = self._resolve_project_path(root_path)
        with self._lock:
            records = self._read()
            if any(_same_path(Path(record.root_path), resolved) for record in records):
                raise ProjectAlreadyExistsError("该项目目录已经注册")
            timestamp = _now()
            record = ProjectRecord(
                id=uuid4().hex,
                name=clean_name,
                root_path=str(resolved),
                branch=None,
                status="idle",
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._write([*records, record])
            return record

    def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        root_path: str | Path | None = None,
    ) -> ProjectRecord:
        with self._lock:
            records = self._read()
            index = next((i for i, item in enumerate(records) if item.id == project_id), None)
            if index is None:
                raise ProjectNotFoundError("项目不存在")
            current = records[index]
            clean_name = self._validate_name(name) if name is not None else current.name
            resolved = self._resolve_project_path(root_path) if root_path is not None else Path(current.root_path)
            if any(
                item.id != project_id and _same_path(Path(item.root_path), resolved)
                for item in records
            ):
                raise ProjectAlreadyExistsError("该项目目录已经注册")
            updated = ProjectRecord(
                id=current.id,
                name=clean_name,
                root_path=str(resolved),
                branch=current.branch,
                status=current.status,
                created_at=current.created_at,
                updated_at=_now(),
            )
            records[index] = updated
            self._write(records)
            return updated

    def delete(self, project_id: str) -> None:
        with self._lock:
            records = self._read()
            remaining = [item for item in records if item.id != project_id]
            if len(remaining) == len(records):
                raise ProjectNotFoundError("项目不存在")
            self._write(remaining)
