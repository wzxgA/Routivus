from __future__ import annotations

import pytest

from routivus.web.errors import WebRateLimitError
from routivus.web.models import SearchRequest, SearchResponse, SearchResult, WebConfig, WebSearchConfig
from routivus.web.search import WebSearchService


class FakeProvider:
    name = "fake"

    def health(self):
        from routivus.web.models import ProviderHealth
        return ProviderHealth("fake", True)

    async def search(self, request):
        return SearchResponse("fake", request.query, (
            SearchResult("one", "https://example.test/a", "x"),
            SearchResult("duplicate", "https://EXAMPLE.test/a#part", "y"),
            SearchResult("two", "https://example.test/b", "z"),
        ))

    async def close(self):
        pass


async def test_search_deduplicates_and_limits_results():
    service = WebSearchService(WebConfig(search=WebSearchConfig(provider="fake", max_results=5)), provider=FakeProvider())
    response = await service.search(SearchRequest(" q ", max_results=2))
    assert response.query == "q"
    assert len(response.results) == 2
    assert response.results[0].source == "fake"


async def test_search_rate_limit_is_shared():
    service = WebSearchService(WebConfig(search=WebSearchConfig(provider="fake", rate_limit_per_minute=1)), provider=FakeProvider())
    await service.search(SearchRequest("one"))
    with pytest.raises(WebRateLimitError):
        await service.search(SearchRequest("two"))
