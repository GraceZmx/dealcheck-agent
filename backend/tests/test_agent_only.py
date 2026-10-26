"""仅 Agent 配置（消融实验 B）测试：用假模型剧本，不联网、不花钱。只用虚构品牌。"""

import json
from pathlib import Path

from app.agent.agent_only import SUBMIT_DECISION, build_task, make_context, market_range, run_agent_only
from app.agent.budget import Budget
from app.agent.tools import tiers_for
from app.config import load_agent_config, load_pricing_config
from app.evalset import grade_run
from app.llm.base import LLMError
from app.llm.mock import MockProvider, assistant, tool_call
from app.models import EvaluationRequest
from app.pipeline import FullEvaluationRequest
from app.rules.engine import evaluate_deal
from app.search.base import SearchResult
from app.search.mock import MockSearchProvider

CONFIG = load_agent_config()
SAMPLE = json.loads((Path(__file__).resolve().parents[2] / "eval" / "sample_case.json").read_text(encoding="utf-8"))
REQ = FullEvaluationRequest.model_validate({"profile": SAMPLE["profile"], "deal": SAMPLE["deal"]})
PRE = evaluate_deal(EvaluationRequest(profile=REQ.profile, deal=REQ.deal))

DECISION = {"decision": "negotiate", "score": 62, "brand_entity_confirmed": True,
            "reasons": ["Offer is fair (s1).", "No kill fee concerns."],
            "severe_flags": [{"type": "safety", "description": "Recall reported (s2)", "source_ids": ["s2"]}]}


def run(script):
    results = [SearchResult(url=f"https://site{i}.example/a", title=f"T{i}", summary=f"summary {i}") for i in range(3)]
    budget = Budget(CONFIG.budget, CONFIG.pricing, "mock-model")
    ctx = make_context(REQ, PRE, MockSearchProvider(results), CONFIG, budget, tiers_for(REQ.profile, load_pricing_config()))
    provider = MockProvider(script)
    result, trace = run_agent_only(REQ, PRE.pricing, ctx, provider, CONFIG)
    return result, trace, provider


def submit(payload=None):
    return tool_call(SUBMIT_DECISION.name, json.dumps(payload or DECISION), "c_submit")


def search():
    return tool_call("web_search", json.dumps({"query": "brand reviews"}), "c1")


def test_search_then_decide():
    result, trace, provider = run([assistant("", search()), assistant("", submit())])
    assert result["config"] == "agent_only"
    assert result["evaluation"]["decision"] == "negotiate" and result["evaluation"]["total_score"] == 62
    assert result["evaluation"]["hard_rules"] == []
    assert [s["id"] for s in result["sources"]] == ["s1", "s2"]
    assert result["agent"]["status"] == "completed"
    assert any(step["type"] == "tool" for step in trace)


def test_only_research_tools_offered():
    _, _, provider = run([assistant("", submit())])
    assert provider.calls[0]["tools"] == ["web_search", "fetch_page", "submit_decision"]


def test_task_has_no_rule_engine_outputs():
    task = build_task(REQ, PRE.pricing)
    for leaked in ("light", "offer_ratio", "position", "hard_rule", "HR-"):
        assert leaked not in task
    assert "market_range" in task and "kill_fee_percent" in task and "requires_upfront_payment" in task


def test_market_range_excludes_exclusivity_and_usage_uplift():
    li = PRE.pricing.line_items
    expected = round(sum(x.unit_price.p50 * x.quantity for x in li), 2)
    assert market_range(PRE.pricing)["p50_usd"] == expected


def test_invented_citation_kept_as_unverified():
    payload = {**DECISION, "reasons": ["Great brand (s7)."], "severe_flags": []}
    result, _, _ = run([assistant("", search()), assistant("", submit(payload))])
    assert result["findings"]["unverified_citations"] == ["s7"]  # 没有护栏：不删除，交给来源检查
    assert result["sources"] == []


def test_invalid_decision_retried_then_fallback():
    bad = tool_call(SUBMIT_DECISION.name, json.dumps({"decision": "maybe"}), "c_bad")
    result, _, _ = run([assistant("", bad), assistant("", bad)])
    assert result["agent"]["status"] == "fallback"
    assert result["evaluation"]["decision"] is None


def test_llm_error_is_fallback():
    result, _, _ = run([LLMError("boom", status=500)])
    assert result["agent"]["status"] == "fallback" and "boom" in result["agent"]["error"]


def test_grading_agent_only_result():
    case = {**SAMPLE, "expected": {**SAMPLE["expected"], "decision": "negotiate",
                                    "hard_rules": {"must_hit": ["HR-7"], "must_not_hit": ["HR-2"]},
                                    "key_risks": [
                                        {"id": "a", "desc": "x", "check": {"type": "term_light", "term": "kill_fee", "light": "green"}},
                                        {"id": "b", "desc": "x", "check": {"type": "hard_rule", "rule": "HR-7"}},
                                        {"id": "c", "desc": "x", "check": {"type": "severe_flag", "flag": "safety"}},
                                    ]}}
    result, _, _ = run([assistant("", search()), assistant("", submit())])
    g = grade_run(case, result)
    assert g["decision_ok"] is True
    assert g["rules_respected"] == [True]  # 封顶类规则：决策没到"接受"
    assert g["false_rules"] == []  # 仅 Agent 没有规则编号，不算误触发
    assert [r["ok"] for r in g["risks"]] == [None, None, True]  # 没有条款灯和规则编号的检查不适用


def test_summary_skips_not_applicable_checks():
    from app.evalset import group_grades, summarize
    case = {**SAMPLE, "expected": {**SAMPLE["expected"], "decision": "negotiate",
                                    "hard_rules": {"must_hit": [], "must_not_hit": []},
                                    "key_risks": [{"id": "a", "desc": "x",
                                                   "check": {"type": "term_light", "term": "kill_fee", "light": "green"}}]}}
    result, _, _ = run([assistant("", submit())])
    s = summarize([case], group_grades([(case["case"]["id"], grade_run(case, result))]))
    assert s["term_light_accuracy"]["total"] == 0  # 仅 Agent 没有条款灯，不计入


def test_grading_fallback_counts_as_wrong():
    case = {**SAMPLE, "expected": {**SAMPLE["expected"], "decision": "negotiate",
                                    "hard_rules": {"must_hit": [], "must_not_hit": []}}}
    result, _, _ = run([LLMError("boom", status=500)])
    g = grade_run(case, result)
    assert g["decision_ok"] is False and g["score_ok"] is None
