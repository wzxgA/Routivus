from __future__ import annotations

import httpx
import pytest

from routivus.web.errors import WebContentError
from routivus.web.fetch import WebFetchService
from routivus.web.models import FetchRequest, WebConfig, WebFetchConfig
from routivus.web.url_policy import URLPolicy


async def test_fetch_static_html_to_wrapped_markdown():
    html = "<html><head><title>Article</title><script>alert(1)</script></head><body><nav>Menu</nav><article><h1>Hello</h1><p>World <a href='https://example.test/x'>link</a></p><pre>code()</pre></article></body></html>"
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=html.encode())))
    service = WebFetchService(WebConfig(fetch=WebFetchConfig()), client=client, policy=URLPolicy(resolve_dns=False))
    try:
        result = await service.fetch(FetchRequest("https://example.test/article"))
        assert result.title == "Article"
        assert "# Hello" in result.markdown
        assert "alert" not in result.markdown
        ok, wrapped = await service.fetch_tool({"url": "https://example.test/article"})
        assert ok and "begin external content" in wrapped
    finally:
        await client.aclose()


async def test_fetch_redirect_and_size_limit():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://example.test/private"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"0123456789")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = WebFetchService(WebConfig(fetch=WebFetchConfig(max_response_bytes=5)), client=client, policy=URLPolicy(resolve_dns=False))
    try:
        with pytest.raises(WebContentError):
            await service.fetch(FetchRequest("https://example.test/start"))
    finally:
        await client.aclose()
