"""智谱 Web Search adapter."""

from __future__ import annotations

from routivus.web.models import ProviderHealth, SearchRequest, SearchResponse, WebSearchConfig
from routivus.web.providers import BaseSearchProvider, result_from_item


class ZhipuSearchProvider(BaseSearchProvider):
    name = "zhipu"

    async def search(self, request: SearchRequest) -> SearchResponse:
        if not self.config.api_base or not self.config.api_key:
            raise ValueError("缺少智谱搜索 API 配置")
        url = self.config.api_base.rstrip("/")
        if not url.endswith("web_search"):
            url += "/web_search"
        payload = {"search_query": request.query, "count": request.max_results}
        if request.recency:
            payload["search_recency_filter"] = request.recency
        data, elapsed = await self.request_json("POST", url, headers={"Authorization": f"Bearer {self.config.api_key}"}, json=payload)
        raw = data.get("search_result") or data.get("results") or data.get("data") or []
        results = tuple(x for item in raw if (x := result_from_item(item, source=self.name, url_keys=("link", "url"), snippet_keys=("content", "snippet", "description"))))
        return SearchResponse(self.name, request.query, results, elapsed_ms=elapsed)


ZhipuProvider = ZhipuSearchProvider
