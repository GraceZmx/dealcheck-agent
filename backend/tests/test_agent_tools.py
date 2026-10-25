"""工具与预算测试：搜索 / 读网页次数上限、只能按编号读网页、成本计算、只读视图。"""

import json

import pytest

from app.agent.budget import Budget
from app.agent.sources import SourceRegistry
from app.agent.tools import SUBMIT_SPEC, TOOL_SPECS, ToolContext, execute_tool, tiers_for
from app.config import load_agent_config, load_pricing_config
from app.llm.base import ToolCall, Usage
from app.models import ContractTerms, CreatorProfile, PricingInput
from app.rules.contract import check_contract
from app.rules.pricing import evaluate_pricing
from app.search.base import FetchResult, SearchError, SearchResult
from app.search.mock import MockSearchProvider

CONFIG = load_agent_config()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make_budget(clock=None, model="deepseek-flash", **limits):
    cfg = CONFIG.budget.model_copy(update=limits)
    return Budget(cfg, CONFIG.pricing, model, clock or FakeClock())


def make_ctx(search=None, budget=None, **profile_overrides) -> ToolContext:
    fields = dict(followers={"instagram": 28_000}, niche="beauty", values=["cruelty-free"],
                  blocked_categories=["alcohol"], blocked_custom=["Energy drinks"], rate_floor_usd=250)
    fields.update(profile_overrides)
    profile = CreatorProfile(**fields)
    pricing = evaluate_pricing(PricingInput(followers=profile.followers, niche=profile.niche,
                                            deliverables=[{"type": "instagram_reel"}], offer_usd=300))
    contract = check_contract(ContractTerms(kill_fee_percent=None, revisions=2))
    return ToolContext(
        profile=profile, pricing=pricing, contract=contract,
        search=search or MockSearchProvider([SearchResult(url=f"https://site{i}.example/a", title=f"T{i}",
                                                          summary="x" * 3000) for i in range(3)]),
        registry=SourceRegistry(), budget=budget or make_budget(), search_settings=CONFIG.search,
        tiers=tiers_for(profile, load_pricing_config()),
    )


def call(ctx, name, **args):
    return json.loads(execute_tool(ctx, ToolCall(id="c", name=name, arguments=json.dumps(args))))


# ---------- web_search ----------


def test_web_search_numbers_results_and_truncates_summary():
    ctx = make_ctx()
    out = call(ctx, "web_search", query="brand reviews")
    assert [r["source_id"] for r in out["results"]] == ["s1", "s2", "s3"]
    assert len(out["results"][0]["summary"]) < 1600 and out["results"][0]["summary"].endswith("[truncated]")
    assert out["searches_left"] == 2
    assert "untrusted" in out["note"]


def test_fourth_search_is_blocked_without_calling_provider():
    ctx = make_ctx()
    for _ in range(3):
        assert "results" in call(ctx, "web_search", query="q")
    out = call(ctx, "web_search", query="q4")
    assert out["error"] == "search_budget_exhausted"
    assert len(ctx.search.searches) == 3


def test_search_budget_comes_from_config():
    ctx = make_ctx(budget=make_budget(max_searches=1))
    call(ctx, "web_search", query="q")
    assert call(ctx, "web_search", query="q")["error"] == "search_budget_exhausted"


def test_long_query_truncated_to_128():
    ctx = make_ctx()
    call(ctx, "web_search", query="a" * 300)
    assert len(ctx.search.searches[0]) == 128


def test_empty_query_rejected_without_using_budget():
    ctx = make_ctx()
    assert call(ctx, "web_search", query="  ")["error"] == "empty_query"
    assert ctx.budget.searches == 0


def test_search_error_returned_to_model_and_counted():
    class Failing(MockSearchProvider):
        def search(self, query, count):
            raise SearchError("HTTP 500", status=500)

    ctx = make_ctx(search=Failing())
    out = call(ctx, "web_search", query="q")
    assert out["error"] == "search_failed" and out["searches_left"] == 2
    assert ctx.budget.billed_searches == 0


def test_empty_search_not_billed():
    ctx = make_ctx(search=MockSearchProvider([]))
    assert call(ctx, "web_search", query="q")["results"] == []
    assert (ctx.budget.searches, ctx.budget.billed_searches) == (1, 0)


def test_same_url_keeps_same_source_id():
    ctx = make_ctx()
    call(ctx, "web_search", query="a")
    out = call(ctx, "web_search", query="b")
    assert [r["source_id"] for r in out["results"]] == ["s1", "s2", "s3"]


# ---------- fetch_page ----------


def _fetch_ctx(**budget_limits):
    pages = {f"https://site{i}.example/a": FetchResult(url=f"https://site{i}.example/a", title="P", content="y" * 9000)
             for i in range(3)}
    results = [SearchResult(url=f"https://site{i}.example/a") for i in range(3)]
    return make_ctx(search=MockSearchProvider(results, pages), budget=make_budget(**budget_limits))


def test_fetch_by_source_id():
    ctx = _fetch_ctx()
    call(ctx, "web_search", query="q")
    out = call(ctx, "fetch_page", source_id="s2")
    assert out["source_id"] == "s2" and out["fetches_left"] == 2
    assert len(out["content"]) < 6100
    assert ctx.search.fetches == ["https://site1.example/a"]


@pytest.mark.parametrize("args", [{"source_id": "s9"}, {"source_id": "https://evil.example"}, {}])
def test_fetch_rejects_unknown_ids_and_urls(args):
    ctx = _fetch_ctx()
    call(ctx, "web_search", query="q")
    out = json.loads(execute_tool(ctx, ToolCall(id="c", name="fetch_page", arguments=json.dumps(args))))
    assert out["error"] == "unknown_source_id"
    assert ctx.search.fetches == [] and ctx.budget.fetches == 0


def test_fourth_fetch_blocked():
    ctx = _fetch_ctx()
    call(ctx, "web_search", query="q")
    for sid in ["s1", "s2", "s3"]:
        assert "content" in call(ctx, "fetch_page", source_id=sid)
    assert call(ctx, "fetch_page", source_id="s1")["error"] == "fetch_budget_exhausted"
    assert len(ctx.search.fetches) == 3


def test_fetch_and_search_budgets_are_separate():
    ctx = _fetch_ctx()
    call(ctx, "web_search", query="q")
    call(ctx, "fetch_page", source_id="s1")
    assert (ctx.budget.searches_left, ctx.budget.fetches_left) == (2, 2)


def test_fetch_error_returned_to_model():
    results = [SearchResult(url="https://blocked.example/a")]
    ctx = make_ctx(search=MockSearchProvider(results, {"https://blocked.example/a": SearchError("HTTP 403", status=403)}))
    call(ctx, "web_search", query="q")
    out = call(ctx, "fetch_page", source_id="s1")
    assert out["error"] == "fetch_failed" and out["fetches_left"] == 2


# ---------- 只读视图 ----------


def test_creator_profile_view_has_no_rate_floor_or_raw_followers():
    out = call(make_ctx(), "get_creator_profile")
    assert out == {"niche": "beauty", "tiers": {"instagram": "micro"}, "values": ["cruelty-free"],
                   "blocked_categories": ["alcohol"], "blocked_custom": ["Energy drinks"]}


def test_benchmark_view():
    out = call(make_ctx(), "get_market_benchmark")
    assert out["adjusted_benchmark_usd"]["p50"] == 546.0 and out["offer_ratio"] == 0.5495


def test_contract_view():
    terms = {t["term"]: t["light"] for t in call(make_ctx(), "check_contract_terms")["terms"]}
    assert terms["kill_fee"] == "red"


def test_unknown_tool_and_bad_arguments():
    ctx = make_ctx()
    assert call(ctx, "send_email")["error"] == "unknown_tool"
    bad = json.loads(execute_tool(ctx, ToolCall(id="c", name="web_search", arguments="not json")))
    assert bad["error"] == "invalid_arguments"


def test_no_tool_can_write_or_send():
    names = {t.name for t in TOOL_SPECS} | {SUBMIT_SPEC.name}
    assert names == {"web_search", "fetch_page", "get_market_benchmark", "check_contract_terms",
                     "get_creator_profile", "submit_findings"}


# ---------- 预算与成本 ----------


def test_cost_counts_reasoning_and_cache():
    b = make_budget()
    b.record_llm(Usage(input_tokens=1_000_000, cached_input_tokens=400_000, output_tokens=100_000, reasoning_tokens=60_000))
    # 600K × 0.30 + 400K × 0.006 + 100K（含思考 60K）× 1.20，单位 USD / 100 万 token
    assert b.cost_usd == pytest.approx(0.18 + 0.0024 + 0.12)
    assert b.cost_cny == pytest.approx(b.cost_usd * 7.1)


def test_cost_includes_billed_search_and_fetch():
    b = make_budget()
    b.record_search(billed=True)
    b.record_search(billed=False)
    b.record_fetch(billed=True)
    assert b.cost_usd == pytest.approx(0.0015)


def test_unknown_model_priced_as_most_expensive():
    assert make_budget(model="future-model").price.output == 3.96


def test_finalize_on_model_call_limit():
    b = make_budget(max_model_calls=3)
    b.record_llm(Usage())
    assert b.finalize_reason() is None
    b.record_llm(Usage())
    assert b.finalize_reason() == "model_call_limit"


def test_finalize_on_time_limit():
    clock = FakeClock()
    b = make_budget(clock=clock)
    clock.now = 119.9
    assert b.finalize_reason() is None
    clock.now = 120
    assert b.finalize_reason() == "time_limit"


def test_finalize_on_cost_limit():
    b = make_budget()
    b.record_llm(Usage(output_tokens=58_000))  # 58K × $1.20/1M × 7.1 ≈ ¥0.494
    assert b.finalize_reason() is None
    b.record_llm(Usage(output_tokens=1_000))  # ≈ ¥0.503
    assert b.finalize_reason() == "cost_limit"


def test_budget_summary_has_cny_and_usd():
    s = make_budget().summary()
    assert {"cost_usd", "cost_cny", "reasoning_tokens", "searches", "fetches"} <= set(s)
