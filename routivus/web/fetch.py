"""Bounded async HTTP fetcher with redirect-by-redirect SSRF checks."""

from __future__ import annotations

import time
import asyncio

import httpx

from routivus.web.errors import WebContentError, WebRateLimitError, WebTimeoutError, user_error
from routivus.web.markdown import html_to_markdown, wrap_external_content
from routivus.web.models import FetchRequest, FetchResponse, WebConfig
from routivus.web.url_policy import URLPolicy


ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class WebFetchService:
    def __init__(self, config: WebConfig, *, client: httpx.AsyncClient | None = None, audit=None,
                 policy: URLPolicy | None = None) -> None:
        self.config = config
        self._client = client if client is not None else httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self.audit = audit
        self.policy = policy or URLPolicy(allowed_ports=config.fetch.allowed_ports)
        self._rate_events: list[float] = []
        self._rate_lock = asyncio.Lock()

    async def _acquire_rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            self._rate_events[:] = [stamp for stamp in self._rate_events if now - stamp < 60]
            if len(self._rate_events) >= self.config.rate_limit_per_minute:
                raise WebRateLimitError(f"每分钟最多 {self.config.rate_limit_per_minute} 次 Web 抓取")
            self._rate_events.append(now)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, request: FetchRequest) -> FetchResponse:
        started = time.monotonic()
        requested = request.url.strip()
        await self._acquire_rate_limit()
        current = await self.policy.avalidate(requested)
        maximum_chars = min(self.config.fetch.max_chars, max(256, int(request.max_chars or self.config.fetch.max_chars)))
        redirects = 0
        try:
            while True:
                try:
                    async with self._client.stream("GET", current.url, headers={"User-Agent": self.config.fetch.user_agent, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9"}, timeout=self.config.fetch.timeout) as response:
                        if response.is_redirect and request.follow_redirects:
                            if redirects >= self.config.fetch.max_redirects:
                                raise WebContentError("重定向次数超过限制")
                            location = response.headers.get("location", "")
                            if not location:
                                raise WebContentError("重定向缺少目标地址")
                            current = await self.policy.avalidate_redirect(current.url, location)
                            redirects += 1
                            continue
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if response.status_code >= 400:
                            raise WebContentError(f"目标网页返回 HTTP {response.status_code}")
                        if content_type and content_type not in ALLOWED_TYPES:
                            raise WebContentError(f"当前只支持网页文本，实际类型为 {content_type}")
                        length = response.headers.get("content-length")
                        if length and length.isdigit() and int(length) > self.config.fetch.max_response_bytes:
                            raise WebContentError("网页响应超过大小限制")
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > self.config.fetch.max_response_bytes:
                                raise WebContentError("网页响应超过大小限制")
                            chunks.append(chunk)
                        raw = b"".join(chunks)
                        encoding = response.encoding or "utf-8"
                except httpx.TimeoutException as exc:
                    raise WebTimeoutError("目标网页请求超时") from exc
                except httpx.HTTPError as exc:
                    raise WebContentError("目标网页连接失败") from exc
                text = raw.decode(encoding, errors="replace")
                if content_type == "text/plain":
                    title, markdown, truncated = "", text, len(text) > maximum_chars
                    markdown = markdown[:maximum_chars]
                else:
                    markdown, title, truncated = html_to_markdown(text, max_chars=maximum_chars)
                if not markdown.strip():
                    raise WebContentError("页面正文提取失败，可复制正文或使用浏览器 MCP")
                elapsed = int((time.monotonic() - started) * 1000)
                result = FetchResponse(requested, current.url, response.status_code, content_type or "text/html", title, markdown, truncated, "", elapsed)
                if self.audit:
                    self.audit.record("web_fetch", host=current.host, requested_url=requested[:2000], final_url=current.url[:2000], status_code=response.status_code, content_type=content_type, chars=len(markdown), ok=True, elapsed_ms=elapsed)
                return result
        except Exception as exc:
            if self.audit:
                self.audit.record("web_fetch", host=current.host, requested_url=requested[:2000], final_url=current.url[:2000], status_code=0, content_type="", chars=0, ok=False, error=str(exc), elapsed_ms=int((time.monotonic() - started) * 1000))
            raise

    async def fetch_tool(self, args: dict) -> tuple[bool, str]:
        try:
            result = await self.fetch(FetchRequest(str(args.get("url", "")), int(args.get("max_chars", self.config.fetch.max_chars)), bool(args.get("follow_redirects", True))))
            return True, wrap_external_content(result.final_url, result.title, result.markdown, max_chars=self.config.fetch.max_chars)
        except Exception as exc:
            return False, user_error(exc)
