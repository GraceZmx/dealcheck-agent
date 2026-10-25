"""假搜索：按查询词返回预设结果，用于测试。"""

from app.search.base import FetchResult, SearchError, SearchProvider, SearchResult


class MockSearchProvider(SearchProvider):
    name = "mock"

    def __init__(
        self,
        results: dict[str, list[SearchResult]] | list[SearchResult] | None = None,
        pages: dict[str, FetchResult | SearchError] | None = None,
    ):
        self._results = results or []
        self._pages = pages or {}
        self.searches: list[str] = []
        self.fetches: list[str] = []

    def search(self, query: str, count: int) -> list[SearchResult]:
        self.searches.append(query)
        if isinstance(self._results, dict):
            return self._results.get(query, [])[:count]
        return self._results[:count]

    def fetch(self, url: str) -> FetchResult:
        self.fetches.append(url)
        page = self._pages.get(url)
        if page is None:
            raise SearchError("not found", status=404)
        if isinstance(page, SearchError):
            raise page
        return page
