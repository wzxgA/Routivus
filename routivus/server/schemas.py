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


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    request_id: str
