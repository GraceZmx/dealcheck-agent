"""DeepSeek Search（/v1/tools/search）和 Fetch（/v1/tools/fetch）。与模型共用同一个 API Key。

重试策略同模型调用：408 / 429 / 5xx / 网络超时等待后重试；403（安全策略拒绝）和 404（读不到内容）不重试。
"""

import time
from collections.abc import Callable

import httpx

from app.search.base import FetchResult, SearchError, SearchProvider, SearchResult

BASE_URL = "https://api.deepseek.com"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class DeepSeekSearchProvider(SearchProvider):
    name = "deepseek"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30,
        max_retries: int = 2,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise SearchError("DeepSeek API key is missing", status=401)
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._base = base_url
        self._client = client or httpx.Client(timeout=timeout)
        self._max_retries = max_retries
        self._sleep = sleep

    def _post(self, path: str, body: dict) -> dict:
        last_error: SearchError | None = None
        for attempt in range(self._max_retries + 1):
            if attempt:
                self._sleep(2 ** (attempt - 1))
            try:
                response = self._client.post(f"{self._base}{path}", headers=self._headers, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = SearchError(f"DeepSeek {path} failed: {type(e).__name__}", retryable=True)
                continue
            if response.status_code == 200:
                return response.json()
            retryable = response.status_code in RETRYABLE_STATUS
            try:
                detail = response.json()["error"]["message"]
            except Exception:
                detail = response.text[:200]
            last_error = SearchError(
                f"DeepSeek {path} HTTP {response.status_code}: {detail}",
                status=response.status_code,
                retryable=retryable,
            )
            if not retryable:
                break
        raise last_error

    def search(self, query: str, count: int) -> list[SearchResult]:
        data = self._post("/v1/tools/search", {"query": query, "count": count})
        results = []
        for r in data.get("results") or []:
            if not r.get("url"):
                continue
            results.append(SearchResult(
                url=r["url"],
                title=r.get("title") or "",
                snippet=r.get("snippet") or "",
                summary=r.get("summary") or "",
                date=r.get("date"),
                authority=r.get("authority"),
                rerank_score=r.get("rerank_score"),
                site_name=r.get("site_name"),
            ))
        return results

    def fetch(self, url: str) -> FetchResult:
        data = self._post("/v1/tools/fetch", {"url": url})
        return FetchResult(url=url, title=data.get("title") or "", content=data.get("content") or "")
