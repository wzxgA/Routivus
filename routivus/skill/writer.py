"""SKILL.md 的受控写入（配置页「技能」页的新建 / 编辑）。

允许的落点只有两处：

- 用户级 ``<user_dir>/skills/<name>/SKILL.md``
- 项目级 ``<project>/.routivus/skills/<name>/SKILL.md``

名称走 `parser.validate_name` 的同一套契约（小写字母/数字/短横线/下划线），
落点再用 `policy.ensure_within` 复核以挡住符号链接逃逸；内置 Skill 由调用方
（服务端）拒绝写入。正文上限沿用 `skills_max_chars`。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from routivus.skill.errors import SkillContentError, SkillSecurityError
from routivus.skill.parser import validate_name
from routivus.skill.policy import ensure_within

LAYERS: tuple[str, ...] = ("user", "project")


@dataclass(frozen=True)
class SkillWriteRequest:
    name: str
    body: str
    description: str = ""
    layer: str = "user"


def skill_root_for(*, user_dir: Path, project_root: Path | None, layer: str) -> Path:
    """写入根目录：只认 user / project 两层，其余一律拒绝。"""
    if layer == "project":
        if project_root is None:
            raise SkillSecurityError("缺少项目上下文，无法写入项目级 Skill")
        return (project_root / ".routivus" / "skills").resolve()
    if layer == "user":
        return (Path(user_dir) / "skills").resolve()
    raise SkillSecurityError(f"未知的写入位置：{layer}")


def render_document(name: str, description: str, body: str) -> str:
    """生成 SKILL.md 文本：首行元信息注释 + 正文（与 parser 的契约一致）。"""
    safe = " ".join(description.split()).replace('"', "'")[:200]
    meta = f"<!-- routivus-skill: name={name}"
    if safe:
        meta += f' description="{safe}"'
    meta += " -->"
    return f"{meta}\n\n{body.strip()}\n"


def write_skill_document(
    request: SkillWriteRequest,
    *,
    user_dir: Path,
    project_root: Path | None,
    max_chars: int,
) -> tuple[Path, bool]:
    """写入 SKILL.md，返回 ``(文件路径, 是否新建)``；失败抛 ``SkillError`` 子类。"""
    name = validate_name(request.name)
    text = render_document(name, request.description, request.body)
    if len(text) > max_chars:
        raise SkillContentError(f"SKILL.md 超过 {max_chars} 字符")
    root = skill_root_for(user_dir=user_dir, project_root=project_root, layer=request.layer)
    folder = ensure_within(root, root / name)
    path = ensure_within(root, folder / "SKILL.md")
    created = not path.exists()
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, created
