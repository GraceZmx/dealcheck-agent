"""Agent Loop 测试：用假模型剧本覆盖正常完成、格式重试、退回、收尾和预算。"""

import json

import pytest

from app.agent.loop import run_agent
from app.config import load_agent_config, load_scoring_config
from app.llm.base import LLMError, Usage
from app.llm.mock import MockProvider, assistant, tool_call
from app.models import DealInput, RiskLevel
from app.search.base import SearchResult
from app.search.mock import MockSearchProvider
from tests.test_agent_tools import make_budget, make_ctx

CONFIG = load_agent_config()
SCORING = load_scoring_config()
DEAL = DealInput(brand_name="Glow Theory", brand_website="https://glowtheory.example",
                 deliverables=[{"type": "instagram_reel"}], offer_usd=300,
                 contract={"kill_fee_percent": None, "revisions": 2})

VALID = {
    "brand_entity_confirmed": True,
    "brand_overview": {"who_they_are": "Skincare brand (s1)."},
    "risk_level": "minor",
    "values_fit": {"level": "high", "summary": "Cruelty-free.", "source_ids": ["s2"]},
    "score_adjustments": [{"item": "brand_reputation", "delta": 2, "reason": "Pays on time.", "source_ids": ["s1"]}],
}


def search_call(query="glow theory reviews", call_id="c1"):
    return tool_call("web_search", json.dumps({"query": query}), call_id)


def submit(payload=None, call_id="c_submit"):
    return tool_call("submit_findings", json.dumps(payload or VALID), call_id)


def ctx_with_results(n=3, **kw):
    results = [SearchResult(url=f"https://site{i}.example/a", title=f"T{i}", summary="summary") for i in range(n)]
    return make_ctx(search=MockSearchProvider(results), **kw)


def run(script, ctx=None, config=CONFIG):
    ctx = ctx or ctx_with_results()
    provider = MockProvider(script)
    return run_agent(DEAL, ctx, provider, config, SCORING), provider, ctx


# ---------- 正常完成 ----------


def test_search_then_submit():
    result, provider, ctx = run([assistant("", search_call()), assistant("", submit())])
    assert result.status == "completed"
    assert result.findings.risk_level == RiskLevel.MINOR
    assert [s.id for s in result.findings.sources] == ["s1", "s2"]  # 只包含引用过的来源
    assert result.findings.sources[0].url == "https://site0.example/a"
    assert result.budget["searches"] == 1 and result.budget["model_calls"] == 2


def test_cited_but_unregistered_source_is_dropped():
    payload = {**VALID, "values_fit": {"level": "high", "summary": "x", "source_ids": ["s9"]}}
    result, _, _ = run([assistant("", search_call()), assistant("", submit(payload))])
    assert [s.id for s in result.findings.sources] == ["s1"]


def test_system_prompt_and_task_sent():
    _, provider, _ = run([assistant("", submit())])
    first = provider.calls[0]["messages"]
    assert first[0].role == "system" and "at most 3 web searches" in first[0].content
    assert "Glow Theory" in first[1].content and '"p50": 546.0' in first[1].content
    assert "rate_floor" not in first[1].content


def test_tool_results_are_sent_back_with_call_ids():
    _, provider, _ = run([assistant("", search_call(call_id="abc")), assistant("", submit())])
    tool_msg = provider.calls[1]["messages"][-1]
    assert tool_msg.role == "tool" and tool_msg.tool_call_id == "abc"
    assert json.loads(tool_msg.content)["results"][0]["source_id"] == "s1"


def test_reasoning_content_kept_in_history():
    _, provider, _ = run([assistant("", search_call(), reasoning="thinking"), assistant("", submit())])
    assistant_msg = provider.calls[1]["messages"][2]
    assert assistant_msg.reasoning_content == "thinking"


def test_parallel_tool_calls_all_answered():
    _, provider, _ = run([
        assistant("", search_call("a", "c1"), tool_call("get_creator_profile", "{}", "c2")),
        assistant("", submit()),
    ])
    answered = [m.tool_call_id for m in provider.calls[1]["messages"] if m.role == "tool"]
    assert answered == ["c1", "c2"]


def test_thinking_off_by_default_in_options():
    _, provider, _ = run([assistant("", submit())])
    assert provider.calls[0]["options"].thinking is False


# ---------- 文字回复与格式错误 ----------


def test_plain_text_reply_gets_nudged():
    result, provider, _ = run([assistant("I think the brand is fine."), assistant("", submit())])
    assert result.status == "completed"
    assert "submit_findings" in provider.calls[1]["messages"][-1].content


def test_invalid_submission_retried_once():
    bad = {"brand_entity_confirmed": "maybe"}
    result, provider, _ = run([assistant("", submit(bad)), assistant("", submit())])
    assert result.status == "completed"
    error = json.loads(provider.calls[1]["messages"][-1].content)
    assert error["error"] == "invalid_findings"
    assert any(t["type"] == "invalid_submission" for t in result.trace)


def test_invalid_submission_twice_falls_back():
    bad = {"risk_level": "terrible"}
    result, _, _ = run([assistant("", submit(bad)), assistant("", submit(bad))])
    assert result.status == "fallback" and result.findings is None
    assert "validation" in result.error


# ---------- 错误与收尾 ----------


def test_llm_error_falls_back():
    result, _, _ = run([LLMError("DeepSeek HTTP 402: Insufficient Balance", status=402)])
    assert result.status == "fallback" and "402" in result.error


def test_model_call_limit_forces_submit_only():
    script = [assistant("", search_call("a", "c1")), assistant("", search_call("b", "c2")), assistant("", submit())]
    result, provider, _ = run(script, ctx=ctx_with_results(budget=make_budget(max_model_calls=3)))
    assert result.status == "completed"
    assert result.finalize_reason == "model_call_limit"
    assert provider.calls[2]["tools"] == ["submit_findings"]
    assert "Budget limit reached" in provider.calls[2]["messages"][-1].content


def test_non_submit_calls_refused_in_final_mode():
    script = [assistant("", search_call()), assistant("", search_call("again", "c9"))]
    result, provider, ctx = run(script, ctx=ctx_with_results(budget=make_budget(max_model_calls=2)))
    assert result.status == "fallback"
    assert ctx.budget.searches == 1  # 收尾阶段的搜索没有执行


def test_cost_limit_triggers_finalize():
    ctx = ctx_with_results(budget=make_budget())
    expensive = Usage(output_tokens=60_000)  # ≈ ¥0.51，超过 ¥0.50
    provider = MockProvider([assistant("", search_call()), assistant("", submit())], usage=expensive)
    result = run_agent(DEAL, ctx, provider, CONFIG, SCORING)
    assert result.status == "completed" and result.finalize_reason == "cost_limit"
    assert provider.calls[1]["tools"] == ["submit_findings"]


def test_search_budget_enforced_inside_loop():
    script = [assistant("", search_call(f"q{i}", f"c{i}")) for i in range(4)] + [assistant("", submit())]
    result, _, ctx = run(script)
    assert ctx.budget.searches == 3
    assert len(ctx.search.searches) == 3
    assert result.status == "completed"


def test_trace_records_llm_and_tool_events():
    result, _, _ = run([assistant("", search_call()), assistant("", submit())])
    types = [t["type"] for t in result.trace]
    assert types == ["llm", "tool", "llm"]
    assert result.trace[0]["usage"]["input_tokens"] == 1000
