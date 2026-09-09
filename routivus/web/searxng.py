"""SearXNG adapter."""

from __future__ import annotations

from routivus.web.models import SearchRequest, SearchResponse
from routivus.web.providers import BaseSearchProvider, result_from_item


class SearXNGSearchProvider(BaseSearchProvider):
    name = "searxng"

    async def search(self, request: SearchRequest) -> SearchResponse:
        if not self.config.api_base:
            raise ValueError("缺少 SearXNG 实例 URL")
        url = self.config.api_base.rstrip("/")
        if not url.endswith("/search"):
            url += "/search"
        params = {"q": request.query, "format": "json", "number_of_results": request.max_results}
        if request.domains:
            params["site"] = ",".join(request.domains)
        data, elapsed = await self.request_json("GET", url, params=params)
        raw = data.get("results") or []
        results = tuple(x for item in raw if (x := result_from_item(item, source=self.name, url_keys=("url", "link"), snippet_keys=("content", "snippet", "description"))))
        return SearchResponse(self.name, request.query, results, elapsed_ms=elapsed)


SearXNGProvider = SearXNGSearchProvider
