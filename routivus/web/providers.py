"""Provider protocol and shared HTTP/error/response normalization helpers."""

from __future__ import annotations

import time
from typing import Any, Protocol

import httpx

from routivus.web.errors import WebProviderError, WebTimeoutError
from routivus.web.models import ProviderHealth, SearchRequest, SearchResponse, SearchResult, WebSearchConfig


class SearchProvider(Protocol):
    name: str

    async def search(self, request: SearchRequest) -> SearchResponse: ...
    async def close(self) -> None: ...
    def health(self) -> ProviderHealth: ...


class BaseSearchProvider:
    name = "unknown"

    def __init__(self, config: WebSearchConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout, follow_redirects=False)
        return self._client

    def health(self) -> ProviderHealth:
        configured = bool(self.config.api_base and (self.config.api_key or self.name == "searxng"))
        return ProviderHealth(self.name, configured, None, "ready" if configured else "missing configuration")

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request_json(self, method: str, url: str, **kwargs: Any) -> tuple[dict, int]:
        started = time.monotonic()
        try:
            response = await self.client.request(method, url, timeout=self.config.timeout, **kwargs)
        except httpx.TimeoutException as exc:
            raise WebTimeoutError("搜索服务请求超时") from exc
        except httpx.HTTPError as exc:
            raise WebProviderError(f"搜索服务连接失败：{type(exc).__name__}") from exc
        if response.status_code in (401, 403):
            raise WebProviderError("provider 认证失败，请检查配置")
        if response.status_code == 429:
            raise WebProviderError("搜索服务限流，请稍后重试")
        if response.status_code >= 500:
            raise WebProviderError("外部搜索服务暂时不可用")
        if response.status_code >= 400:
            raise WebProviderError(f"搜索服务请求失败（HTTP {response.status_code}）")
        try:
            value = response.json()
        except ValueError as exc:
            raise WebProviderError("搜索服务返回了无效 JSON") from exc
        if not isinstance(value, dict):
            raise WebProviderError("搜索服务返回格式不正确")
        return value, int((time.monotonic() - started) * 1000)


def result_from_item(item: Any, *, source: str, url_keys=("url", "link"), snippet_keys=("snippet", "content", "description")) -> SearchResult | None:
    if not isinstance(item, dict):
        return None
    title = next((item.get(k) for k in ("title", "name") if item.get(k)), "")
    url = next((item.get(k) for k in url_keys if item.get(k)), "")
    snippet = next((item.get(k) for k in snippet_keys if item.get(k)), "")
    published = item.get("published_at") or item.get("publishedDate") or item.get("date")
    if not isinstance(title, str) or not isinstance(url, str) or not url:
        return None
    return SearchResult(str(title), str(url), str(snippet or ""), str(published) if published else None, source)
