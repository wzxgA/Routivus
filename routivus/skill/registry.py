"""Skill registry: metadata discovery at startup and body loading on demand."""

from __future__ import annotations

import time
from pathlib import Path

from routivus.skill.errors import SkillContentError, SkillDisabledError, SkillNotFoundError, SkillParseError, user_error
from routivus.skill.loader import SkillLoader
from routivus.skill.models import SkillConfig, SkillDocument, SkillInfo, SkillLoadRequest, SkillReference, SkillRoot
from routivus.skill.parser import META_RE, read_body, parse_metadata
from routivus.skill.policy import validate_reference_path
from routivus.skill.prompt import build_index, wrap_document


class SkillRegistry:
    def __init__(self, *, project_root: str | Path, config: SkillConfig | None = None,
                 config_manager=None, builtin_root: str | Path | None = None,
                 audit=None) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = config or SkillConfig()
        self.config_manager = config_manager
        self.audit = audit
        builtin = Path(builtin_root) if builtin_root else Path(__file__).resolve().parents[1] / "skills"
        user_dir = Path(getattr(config_manager, "user_dir", Path.home() / ".routivus"))
        self.roots = (
            SkillRoot("builtin", builtin.resolve()),
            SkillRoot("user", (user_dir / "skills").resolve()),
            SkillRoot("project", (self.project_root / ".routivus" / "skills").resolve()),
        )
        self._infos: tuple[SkillInfo, ...] = ()
        self._cache: dict[tuple[str, tuple[str, ...]], SkillDocument] = {}
        self._cache_signatures: dict[tuple[str, tuple[str, ...]], tuple[tuple[int, int] | None, ...]] = {}
        self.reload()

    def reload(self) -> None:
        overrides = self.config_manager.enabled_overrides() if self.config_manager else {}
        self._infos = SkillLoader(self.roots, self.config).discover(overrides)
        self._cache.clear()
        self._cache_signatures.clear()

    def list(self, include_disabled: bool = True) -> tuple[SkillInfo, ...]:
        if not include_disabled:
            return tuple(item for item in self._infos if item.enabled and item.valid)
        return self._infos

    def get(self, name: str) -> SkillInfo | None:
        return next((item for item in self._infos if item.name == name), None)

    def index_text(self) -> str:
        if not self.config.enabled:
            return ""
        return build_index(self._infos, max_items=self.config.max_index_items, max_chars=self.config.max_index_chars)

    def load(self, request: SkillLoadRequest | str) -> SkillDocument:
        if isinstance(request, str):
            request = SkillLoadRequest(request)
        info = self.get(request.name)
        if info is None:
            raise SkillNotFoundError(request.name)
        if not info.valid:
            raise SkillParseError(f"{request.name}：{info.error}")
        if not info.enabled:
            raise SkillDisabledError(request.name)
        references = tuple(request.references)
        if len(references) > self.config.max_references:
            raise SkillParseError(f"单次最多加载 {self.config.max_references} 个 reference")
        cache_key = (info.name, references)
        reference_paths = tuple(
            validate_reference_path(info.root, raw_path) for raw_path in references
        )
        paths = (info.root / "SKILL.md",) + reference_paths
        signatures = tuple(self._signature(path) for path in paths)
        cached = self._cache.get(cache_key)
        if (
            cached is not None
            and self._cache_signatures.get(cache_key) == signatures
            and all(signature is not None for signature in signatures)
        ):
            return cached
        body, truncated = read_body(info.root / "SKILL.md", max_chars=self.config.max_skill_chars)
        # Remove the optional metadata comment before returning the task text.
        body = META_RE.sub("", body, count=1).strip()
        refs = []
        total = len(body)
        for raw_path, path in zip(references, reference_paths):
            content, ref_truncated = read_body(path, max_chars=self.config.max_reference_chars)
            total += len(content)
            if total > self.config.max_loaded_chars:
                raise SkillContentError(f"单次加载内容超过 {self.config.max_loaded_chars} 字符")
            refs.append(SkillReference(raw_path.replace("\\", "/"), content, ref_truncated))
        if total > self.config.max_loaded_chars:
            raise SkillContentError(f"单次加载内容超过 {self.config.max_loaded_chars} 字符")
        document = SkillDocument(info, body, tuple(refs), truncated)
        self._cache[cache_key] = document
        self._cache_signatures[cache_key] = tuple(self._signature(path) for path in paths)
        return document

    @staticmethod
    def _signature(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def set_enabled(self, name: str, enabled: bool) -> bool:
        info = self.get(name)
        if info is None or self.config_manager is None:
            return False
        self.config_manager.set_enabled(name, enabled, project=info.source == "project")
        self.reload()
        if self.audit:
            self.audit.record("skill_enabled", name=name, source=info.source, enabled=enabled, ok=True)
        return True

    def format_list(self) -> str:
        rows = ["Skills:"]
        for info in self._infos:
            status = "启用" if info.enabled and info.valid else ("禁用" if not info.enabled else "无效")
            detail = f"（{info.error}）" if info.error else ""
            rows.append(f"- {info.name} · {info.source} · {status} · {info.description}{detail}")
        return "\n".join(rows) if len(rows) > 1 else "没有发现 Skill。"

    async def load_tool(self, args: dict) -> tuple[bool, str]:
        started = time.monotonic()
        name = str(args.get("name", "")).strip()
        raw_refs = args.get("references", ()) or ()
        refs = tuple(str(item) for item in raw_refs) if isinstance(raw_refs, (list, tuple)) else ()
        try:
            if not self.config.enabled:
                raise SkillDisabledError("Skill 总开关已关闭")
            document = self.load(SkillLoadRequest(name, refs))
            output = wrap_document(document)
            if self.audit:
                self.audit.record("skill_load", name=name, source=document.info.source,
                                  references=list(refs), chars=len(output), truncated=document.truncated,
                                  ok=True, elapsed_ms=int((time.monotonic() - started) * 1000))
            return True, output
        except Exception as exc:
            if self.audit:
                info = self.get(name)
                self.audit.record("skill_load", name=name, source=info.source if info else "unknown",
                                  references=list(refs), chars=0, truncated=False, ok=False,
                                  error=str(exc), elapsed_ms=int((time.monotonic() - started) * 1000))
            return False, user_error(exc)

    def manual_load(self, name: str, references: tuple[str, ...] = ()) -> tuple[bool, str]:
        started = time.monotonic()
        try:
            document = self.load(SkillLoadRequest(name, references))
            output = wrap_document(document)
            if self.audit:
                self.audit.record(
                    "skill_load", name=name, source=document.info.source,
                    references=list(references), chars=len(output),
                    truncated=document.truncated, ok=True,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            return True, output
        except Exception as exc:
            if self.audit:
                info = self.get(name)
                self.audit.record(
                    "skill_load", name=name, source=info.source if info else "unknown",
                    references=list(references), chars=0, truncated=False, ok=False,
                    error=str(exc), elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            return False, user_error(exc)
