"""Data contracts shared by web tools and providers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderHealth:
    name: str
    configured: bool
    healthy: bool | None = None
    detail: str = ""


@dataclass(frozen=True)
class WebSearchConfig:
    provider: str = "none"
    api_base: str | None = None
    api_key_env: str | None = None
    api_key: str | None = None
    timeout: float = 15.0
    max_results: int = 5
    rate_limit_per_minute: int = 30
    enabled: bool = True


@dataclass(frozen=True)
class WebFetchConfig:
    timeout: float = 15.0
    max_response_bytes: int = 2 * 1024 * 1024
    max_chars: int = 32_000
    max_redirects: int = 5
    allowed_ports: tuple[int, ...] = (80, 443)
    user_agent: str = "XG-CLI/0.1 (+https://github.com/xg-cli)"


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = True
    search: WebSearchConfig = field(default_factory=WebSearchConfig)
    fetch: WebFetchConfig = field(default_factory=WebFetchConfig)
    providers: dict[str, dict] = field(default_factory=dict)
    rate_limit_per_minute: int = 30


@dataclass(frozen=True)
class SearchRequest:
    query: str
    max_results: int = 5
    recency: str | None = None
    domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class SearchResponse:
    provider: str
    query: str
    results: tuple[SearchResult, ...] = ()
    truncated: bool = False
    warning: str = ""
    elapsed_ms: int = 0


@dataclass(frozen=True)
class FetchRequest:
    url: str
    max_chars: int = 32_000
    follow_redirects: bool = True


@dataclass(frozen=True)
class FetchResponse:
    requested_url: str
    final_url: str = ""
    status_code: int = 0
    content_type: str = ""
    title: str = ""
    markdown: str = ""
    truncated: bool = False
    warning: str = ""
    elapsed_ms: int = 0
