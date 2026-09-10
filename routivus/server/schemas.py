"""HTTP request and response schemas for the server foundation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    root_path: str = Field(min_length=1, max_length=4096)

    @field_validator("name", "root_path")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value


class ProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    root_path: str | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("name", "root_path")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value


class ProjectStats(BaseModel):
    sessions: int = 0
    notes: int = 0
    calls_today: int = 0


class ProjectResponse(BaseModel):
    id: str
    name: str
    root_path: str
    branch: str | None
    status: Literal["idle", "working", "error", "archived"]
    created_at: datetime
    updated_at: datetime
    stats: ProjectStats = Field(default_factory=ProjectStats)


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="新建会话", min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("会话标题不能为空")
        return value


class SessionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def strip_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("会话标题不能为空")
        return value


class SessionResponse(BaseModel):
    id: str
    project_id: str
    title: str
    status: Literal["idle", "running", "waiting_approval", "completed", "failed", "cancelled"]
    active_provider: str
    active_model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    created_at: datetime
    updated_at: datetime


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "assistant", "tool", "system"]
    content: str
    tool_name: str | None
    tool_args: dict | None
    tool_result: str | None
    created_at: datetime


class NoteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    body_markdown: str = Field(default="", max_length=2_000_000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    project_id: str | None = None

    @field_validator("title")
    @classmethod
    def strip_note_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("笔记标题不能为空")
        return value

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(len(value) > 50 for value in cleaned):
            raise ValueError("笔记标签不能超过 50 个字符")
        return cleaned


class NoteUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body_markdown: str | None = Field(default=None, max_length=2_000_000)
    tags: list[str] | None = Field(default=None, max_length=50)
    project_id: str | None = None
    version: int | None = Field(default=None, ge=1)

    @field_validator("title")
    @classmethod
    def strip_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("笔记标题不能为空")
        return value

    @field_validator("tags")
    @classmethod
    def clean_optional_tags(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(len(value) > 50 for value in cleaned):
            raise ValueError("笔记标签不能超过 50 个字符")
        return cleaned


class NoteResponse(BaseModel):
    id: str
    project_id: str | None
    project_name: str | None
    title: str
    body_markdown: str
    preview: str
    tags: list[str]
    pinned: bool
    created_at: datetime
    updated_at: datetime
    version: int


class PinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: bool | None = None


class ActivityResponse(BaseModel):
    date: str
    count: int
    projects: dict[str, int]


class NoteStatsResponse(BaseModel):
    total: int
    global_notes: int
    project_notes: int
    by_project: dict[str, int]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    request_id: str


# ---- Web Console 配置接口（Provider / SmartRouter） ----
# 没有这组接口，用户就只能靠 REPL 或 CLI 配 provider；而本仓库两者都没有，
# 桌面版首次打开会直接不可用。API Key 只写入、不回显（仅返回脱敏值）。


class ProviderView(BaseModel):
    name: str
    display_name: str | None = None
    api_base: str
    default_model: str
    models: list[str] = Field(default_factory=list)
    has_key: bool
    api_key_masked: str = ""
    is_base: bool
    layer: str


class TierView(BaseModel):
    name: str
    provider: str
    model: str
    configured: bool


class ConfigSnapshot(BaseModel):
    active_provider: str
    active_model: str
    providers: list[ProviderView]
    tiers: list[TierView]
    smart_router_enabled: bool
    user_dir: str
    legacy_user_dir: str | None = None
    desktop: bool = False


class ProviderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    api_base: str = Field(min_length=1, max_length=2048)
    default_model: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=100)
    api_key: str | None = Field(default=None, max_length=4096)
    set_base: bool = False


class ProviderUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_base: str | None = Field(default=None, min_length=1, max_length=2048)
    default_model: str | None = Field(default=None, min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=100)


class ProviderKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=1, max_length=4096)
    # 界面已把"覆盖旧值"做成显式动作，所以默认允许覆盖。
    overwrite: bool = True


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=200)


class ActiveProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=200)


class TierRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=200)


class SmartRouterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class RootGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)


class DesktopInfoResponse(BaseModel):
    desktop: bool
    user_dir: str
    legacy_user_dir: str | None = None
    static_dir: str | None = None
    allowed_roots: list[str]
