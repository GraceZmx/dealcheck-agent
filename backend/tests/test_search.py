"""搜索封装层测试：用 httpx.MockTransport 模拟网络。"""

import httpx
import pytest

from app.config import load_agent_config
from app.search.base import FetchResult, SearchError, SearchResult
from app.search.deepseek import DeepSeekSearchProvider
from app.search.factory import create_search_provider
from app.search.mock import MockSearchProvider
from app.settings import Settings

FAKE_KEY = "sk-test-not-a-real-key"


def make_provider(handler):
    seen: list[httpx.Request] = []

    def record(request):
        seen.append(request)
        return handler(request, len(seen))

    client = httpx.Client(transport=httpx.MockTransport(record))
    return DeepSeekSearchProvider(FAKE_KEY, client=client, sleep=lambda s: None), seen


RESULT = {
    "url": "https://news.example/a", "title": "A", "snippet": "s", "summary": "long summary",
    "content": "", "date": "2026-07-10T16:00:00+00:00", "authority": 3, "rerank_score": 1.4,
    "site_name": "News", "site_icon": None,
}


def test_search_parses_results_and_sends_query():
    provider, seen = make_provider(lambda r, n: httpx.Response(200, json={"results": [RESULT, {**RESULT, "url": ""}]}))
    results = provider.search("brand reviews", 5)
    assert results == [SearchResult(url="https://news.example/a", title="A", snippet="s", summary="long summary",
                                    date="2026-07-10T16:00:00+00:00", authority=3, rerank_score=1.4, site_name="News")]
    assert seen[0].url == "https://api.deepseek.com/v1/tools/search"
    assert seen[0].read() == b'{"query":"brand reviews","count":5}'
    assert seen[0].headers["authorization"] == f"Bearer {FAKE_KEY}"


def test_search_empty_results():
    provider, _ = make_provider(lambda r, n: httpx.Response(200, json={"results": []}))
    assert provider.search("nothing", 5) == []


def test_fetch_parses_page():
    provider, seen = make_provider(lambda r, n: httpx.Response(200, json={"title": "T", "content": "# Hello"}))
    assert provider.fetch("https://news.example/a") == FetchResult(url="https://news.example/a", title="T", content="# Hello")
    assert seen[0].url == "https://api.deepseek.com/v1/tools/fetch"


@pytest.mark.parametrize("status", [403, 404, 400, 401, 402])
def test_fetch_non_retryable_errors(status):
    provider, seen = make_provider(lambda r, n: httpx.Response(status, json={"error": {"message": "blocked"}}))
    with pytest.raises(SearchError) as e:
        provider.fetch("https://news.example/a")
    assert len(seen) == 1 and e.value.status == status and not e.value.retryable


@pytest.mark.parametrize("status", [429, 500, 504])
def test_retryable_errors_retried(status):
    def handler(r, n):
        return httpx.Response(status, json={"error": {"message": "busy"}}) if n == 1 else httpx.Response(200, json={"results": [RESULT]})

    provider, seen = make_provider(handler)
    assert len(provider.search("q", 5)) == 1
    assert len(seen) == 2


def test_timeout_retried_then_raises():
    def handler(r, n):
        raise httpx.ConnectTimeout("slow", request=r)

    provider, seen = make_provider(handler)
    with pytest.raises(SearchError) as e:
        provider.search("q", 5)
    assert len(seen) == 3 and e.value.retryable


def test_error_never_contains_key():
    provider, _ = make_provider(lambda r, n: httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(SearchError) as e:
        provider.search("q", 5)
    assert FAKE_KEY not in str(e.value)


def test_mock_search_provider():
    mock = MockSearchProvider(
        {"a": [SearchResult(url="https://x.example/1")]},
        {"https://x.example/1": FetchResult(url="https://x.example/1", content="hi")},
    )
    assert mock.search("a", 5)[0].url == "https://x.example/1"
    assert mock.search("b", 5) == []
    assert mock.fetch("https://x.example/1").content == "hi"
    with pytest.raises(SearchError):
        mock.fetch("https://x.example/missing")
    assert mock.searches == ["a", "b"]


def test_factory():
    assert isinstance(create_search_provider(Settings(deepseek_api_key=FAKE_KEY), load_agent_config()), DeepSeekSearchProvider)
    with pytest.raises(SearchError):
        create_search_provider(Settings(deepseek_api_key=None), load_agent_config())
    with pytest.raises(SearchError):
        create_search_provider(Settings(search_provider="nope", deepseek_api_key=FAKE_KEY), load_agent_config())


# ---------- 来源登记表 ----------

from app.agent.sources import SourceRegistry  # noqa: E402


def test_registry_numbers_and_dedupes():
    reg = SourceRegistry()
    assert reg.register(SearchResult(url="https://a.example/1", title="A")) == "s1"
    assert reg.register(SearchResult(url="https://b.example/2")) == "s2"
    assert reg.register(SearchResult(url="https://a.example/1")) == "s1"
    assert len(reg) == 2 and "s2" in reg and "s3" not in reg
    assert reg.get("s1").title == "A" and reg.get("s9") is None


def test_registry_refs_filter():
    reg = SourceRegistry()
    reg.register(SearchResult(url="https://a.example/1", title="A"))
    reg.register(SearchResult(url="https://b.example/2"))
    assert [r.id for r in reg.refs()] == ["s1", "s2"]
    assert [r.url for r in reg.refs({"s2", "s7"})] == ["https://b.example/2"]
    assert reg.refs({"s1"})[0].title == "A"
