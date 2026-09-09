"""Parser for the deliberately small SKILL.md metadata contract."""

from __future__ import annotations

import re
from pathlib import Path

from routivus.skill.errors import SkillContentError, SkillParseError


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
META_RE = re.compile(r"<!--\s*routivus-skill:\s*(.*?)\s*-->", re.IGNORECASE | re.DOTALL)
FIELD_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)=(?:\"((?:[^\"\\]|\\.)*)\"|'([^']*)'|(\S+))")
TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".json", ".toml", ".ini", ".cfg", ".yaml", ".yml"}


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise SkillParseError("名称只能包含小写字母、数字、短横线和下划线，长度 1～64")
    return name


def _fields(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in FIELD_RE.finditer(raw):
        value = next((item for item in match.groups()[1:] if item is not None), "")
        # Metadata is UTF-8 text.  ``unicode_escape`` would reinterpret every
        # non-ASCII byte and can corrupt Chinese descriptions.  Only unescape
        # the two escapes that are meaningful in this small contract.
        value = re.sub(r'\\(["\\])', r"\1", value)
        fields[match.group(1).lower()] = value
    # The metadata is intentionally strict: every non-whitespace character
    # must belong to a known key/value pair.
    if not fields or FIELD_RE.sub("", raw).strip():
        raise SkillParseError("routivus-skill 元信息格式错误")
    return fields


def _description(body: str) -> str:
    for line in body.splitlines():
        value = line.strip()
        if not value or value.startswith("#") or value.startswith("<!--"):
            continue
        return re.sub(r"[*_`]+", "", value)[:500]
    return "未提供描述"


def parse_metadata(name: str, text: str, *, source: str, root: Path) -> dict:
    validate_name(name)
    matches = list(META_RE.finditer(text[:16_384]))
    if len(matches) > 1:
        raise SkillParseError("SKILL.md 只能包含一段 routivus-skill 元信息")
    values: dict[str, str] = {}
    if matches:
        values = _fields(matches[0].group(1))
        declared = values.get("name")
        if declared and declared != name:
            raise SkillParseError(f"元信息 name={declared} 与目录名 {name} 不一致")
    body = META_RE.sub("", text, count=1).strip() if matches else text.strip()
    return {
        "name": name,
        "description": values.get("description") or _description(body),
        "version": values.get("version") or None,
        "source": source,
        "root": root,
    }


def read_metadata(skill_root: Path, *, name: str, source: str) -> dict:
    path = skill_root / "SKILL.md"
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            text = handle.read(16_384)
    except UnicodeError as exc:
        raise SkillParseError("SKILL.md 不是有效 UTF-8") from exc
    except OSError as exc:
        raise SkillParseError("无法读取 SKILL.md") from exc
    if not text.strip():
        raise SkillParseError("SKILL.md 不能为空")
    return parse_metadata(name, text, source=source, root=skill_root)


def read_body(path: Path, *, max_chars: int) -> tuple[str, bool]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            text = handle.read(max_chars + 1)
    except UnicodeError as exc:
        raise SkillParseError(f"{path.name} 不是有效 UTF-8") from exc
    except OSError as exc:
        raise SkillParseError(f"无法读取 {path.name}") from exc
    if len(text) > max_chars:
        raise SkillContentError(f"{path.name} 超过 {max_chars} 字符")
    return text, False
