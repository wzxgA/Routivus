"""Safe, read-only web capabilities for XG."""

from routivus.web.fetch import WebFetchService
from routivus.web.models import (
    FetchRequest,
    FetchResponse,
    ProviderHealth,
    SearchRequest,
    SearchResponse,
    SearchResult,
    WebFetchConfig,
    WebSearchConfig,
    WebConfig,
)
from routivus.web.search import WebSearchService

__all__ = [
    "FetchRequest", "FetchResponse", "ProviderHealth", "SearchRequest",
    "SearchResponse", "SearchResult", "WebFetchConfig", "WebSearchConfig",
    "WebConfig", "WebFetchService", "WebSearchService",
]
