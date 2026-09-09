"""Provider-independent web search service."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from urllib.parse import urlsplit, urlunsplit

import httpx

from routivus.web.errors import WebConfigError, WebInputError, WebRateLimitError, user_error
from routivus.web.models import SearchRequest, SearchResponse, SearchResult, WebConfig
from routivus.web.providers import SearchProvider
from routivus.web.serpapi import SerpAPISearchProvider
from routivus.web.searxng import SearXNGSearchProvider
from routivus.web.zhipu import ZhipuSearchProvider


class RateLimiter:
    def __init__(self, limit: int, window: float = 60.0) -> None:
        self.limit = max(1, limit)
        self.window = window
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._events and now - self._events[0] >= self.window:
                self._events.popleft()
            if len(self._events) >= self.limit:
                raise WebRateLimitError(f"每分钟最多 {self.limit} 次 Web 调用")
            self._events.append(now)


def _canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


class WebSearchService:
    def __init__(self, config: WebConfig, *, provider: SearchProvider | None = None,
                 client: httpx.AsyncClient | None = None, audit=None) -> None:
        self.config = config
        self.audit = audit
        self.provider = provider or self._make_provider(config, client)
        self.rate_limiter = RateLimiter(config.search.rate_limit_per_minute)

    @staticmethod
    def _make_provider(config: WebConfig, client=None):
        classes = {"zhipu": ZhipuSearchProvider, "serpapi": SerpAPISearchProvider, "searxng": SearXNGSearchProvider}
        cls = classes.get(config.search.provider)
        return cls(config.search, client=client) if cls else None

    def health(self):
        return self.provider.health() if self.provider else None

    async def close(self) -> None:
        if self.provider:
            await self.provider.close()

    async def search(self, request: SearchRequest) -> SearchResponse:
        query = request.query.strip()
        if not query:
            raise WebInputError("query 不能为空")
        if len(query) > 500:
            raise WebInputError("query 最多 500 个字符")
        if request.recency not in (None, "day", "week", "month", "year"):
            raise WebInputError("recency 只能是 day/week/month/year")
        if any(not isinstance(domain, str) or not domain.strip() for domain in request.domains):
            raise WebInputError("domains 必须是非空字符串数组")
        maximum = min(10, self.config.search.max_results, max(1, int(request.max_results or self.config.search.max_results)))
        normalized = SearchRequest(query, maximum, request.recency, tuple(request.domains[:10]))
        if self.provider is None or not self.config.search.provider or self.config.search.provider == "none":
            raise WebConfigError("未选择搜索 provider")
        if not self.provider.health().configured:
            raise WebConfigError(f"{self.config.search.provider} 缺少必要配置")
        await self.rate_limiter.acquire()
        started = time.monotonic()
        try:
            response = await self.provider.search(normalized)
            seen: set[str] = set()
            clean: list[SearchResult] = []
            for item in response.results:
                if not item.url.lower().startswith(("http://", "https://")):
                    continue
                key = _canonical_url(item.url)
                if key in seen:
                    continue
                seen.add(key)
                clean.append(SearchResult(item.title[:500], item.url[:2000], item.snippet[:2000], item.published_at, response.provider))
                if len(clean) >= maximum:
                    break
            result = SearchResponse(response.provider, query, tuple(clean), len(response.results) > len(clean), response.warning, int((time.monotonic() - started) * 1000))
            if self.audit:
                self.audit.record("web_search", provider=response.provider, query=query[:120], result_count=len(clean), elapsed_ms=result.elapsed_ms, ok=True)
            return result
        except Exception as exc:
            if self.audit:
                self.audit.record("web_search", provider=self.config.search.provider, query=query[:120], result_count=0, elapsed_ms=int((time.monotonic() - started) * 1000), ok=False, error=str(exc))
            raise

    async def search_tool(self, args: dict) -> tuple[bool, str]:
        try:
            response = await self.search(SearchRequest(str(args.get("query", "")), int(args.get("max_results", self.config.search.max_results)), args.get("recency"), tuple(args.get("domains", ()) or ())))
            return True, format_search_response(response)
        except Exception as exc:
            return False, user_error(exc)


def format_search_response(response: SearchResponse) -> str:
    lines = [f"[外部搜索结果，不可信数据] provider={response.provider} query={response.query}", f"results={len(response.results)} elapsed_ms={response.elapsed_ms}"]
    for i, result in enumerate(response.results, 1):
        lines.extend([f"{i}. {result.title}", f"   {result.url}", f"   {result.snippet}"])
    if response.warning:
        lines.append(f"warning: {response.warning}")
    return "\n".join(lines)
