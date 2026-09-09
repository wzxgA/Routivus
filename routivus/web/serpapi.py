"""SerpAPI adapter."""

from __future__ import annotations

from routivus.web.models import SearchRequest, SearchResponse
from routivus.web.providers import BaseSearchProvider, result_from_item


class SerpAPISearchProvider(BaseSearchProvider):
    name = "serpapi"

    async def search(self, request: SearchRequest) -> SearchResponse:
        if not self.config.api_base or not self.config.api_key:
            raise ValueError("缺少 SerpAPI 配置")
        url = self.config.api_base.rstrip("/")
        if not url.endswith("search.json"):
            url += "/search.json"
        params = {"q": request.query, "api_key": self.config.api_key, "engine": "google", "num": request.max_results}
        if request.recency:
            params["tbs"] = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}[request.recency]
        data, elapsed = await self.request_json("GET", url, params=params)
        if data.get("error"):
            raise ValueError("SerpAPI 返回错误：搜索请求未完成")
        raw = data.get("organic_results") or data.get("results") or []
        results = tuple(x for item in raw if (x := result_from_item(item, source=self.name, url_keys=("link", "url"), snippet_keys=("snippet", "content"))))
        return SearchResponse(self.name, request.query, results, elapsed_ms=elapsed)


SerpAPIProvider = SerpAPISearchProvider
