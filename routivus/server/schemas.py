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
