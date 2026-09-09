from __future__ import annotations

import httpx

from routivus.web.models import SearchRequest, WebSearchConfig
from routivus.web.serpapi import SerpAPISearchProvider
from routivus.web.searxng import SearXNGSearchProvider
from routivus.web.zhipu import ZhipuSearchProvider


async def test_provider_response_shapes_are_normalized():
    responses = {
        "zhipu": httpx.Response(200, json={"search_result": [{"title": "A", "link": "https://a.test", "content": "one"}]}),
        "serpapi": httpx.Response(200, json={"organic_results": [{"title": "B", "link": "https://b.test", "snippet": "two"}]}),
        "searxng": httpx.Response(200, json={"results": [{"title": "C", "url": "https://c.test", "content": "three"}]}),
    }

    def handler(request: httpx.Request):
        return responses[request.url.host]

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        configs = [
            (ZhipuSearchProvider, WebSearchConfig("zhipu", "https://zhipu", api_key="key")),
            (SerpAPISearchProvider, WebSearchConfig("serpapi", "https://serpapi", api_key="key")),
            (SearXNGSearchProvider, WebSearchConfig("searxng", "https://searxng")),
        ]
        for cls, config in configs:
            result = await cls(config, client=client).search(SearchRequest("q"))
            assert len(result.results) == 1
            assert result.results[0].url.startswith("https://")
    finally:
        await client.aclose()
