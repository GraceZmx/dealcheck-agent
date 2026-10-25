"""完整流程测试：规则预检 → Agent → 打分决策 → 邮件；跳过、退回和接口。全部使用假模型和假搜索。"""

import json

from fastapi.testclient import TestClient

from app.llm.base import LLMError
from app.llm.mock import MockProvider, assistant, tool_call
from app.main import app, get_providers
from app.models import Decision
from app.pipeline import FullEvaluationRequest, run_full_evaluation
from app.search.base import SearchResult
from app.search.mock import MockSearchProvider
from tests.test_engine import EXAMPLES

SUBMIT = {
    "brand_entity_confirmed": True,
    "brand_overview": {"who_they_are": "Skincare brand (s1)."},
    "risk_level": "minor",
    "values_fit": {"level": "high", "summary": "Cruelty-free.", "source_ids": ["s2"]},
    "score_adjustments": [{"item": "brand_reputation", "delta": 2, "reason": "Pays on time.", "source_ids": ["s1"]}],
}
EMAIL = json.dumps({"subject": "Re: {{brand_name}}",
                    "body": "Hi! Rate {{counter_rate}}. {{ask_1}} {{ask_2}}\n{{creator_name}}"})


def request(name="2_negotiate_maya.json", **overrides) -> FullEvaluationRequest:
    raw = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    raw.pop("_description", None)
    raw["agent_findings"] = None
    raw.update(overrides)
    return FullEvaluationRequest.model_validate(raw)


def search():
    return MockSearchProvider([SearchResult(url=f"https://site{i}.example/a", title=f"T{i}", summary="s") for i in range(3)])


def agent_script():
    return [
        assistant("", tool_call("web_search", json.dumps({"query": "glow theory reviews"}))),
        assistant("", tool_call("submit_findings", json.dumps(SUBMIT))),
        assistant(EMAIL),
    ]


def test_full_pipeline_with_agent():
    llm = MockProvider(agent_script())
    result, trace = run_full_evaluation(request(creator_name="Maya"), llm, search())
    assert result.agent.status == "completed"
    assert result.evaluation.decision == Decision.NEGOTIATE
    assert result.evaluation.score.brand_fit.final == 14  # 12 + 2（口碑）；values_fit 没有调分
    assert [s.id for s in result.sources] == ["s1", "s2"]
    assert result.email.generated_by == "llm" and "$830" in result.email.body and "Maya" in result.email.body
    assert result.agent.usage["model_calls"] == 3  # 2 次 Agent + 1 次邮件
    assert result.agent.usage["cost_cny"] > 0
    assert [t["type"] for t in trace] == ["llm", "tool", "llm"]


def test_agent_findings_change_the_decision():
    """Agent 有据发现品牌涉及拒接品类 → HR-1 → 拒绝。"""
    findings = {**SUBMIT, "detected_categories": [{"category": "alcohol", "source_ids": ["s1"]}]}
    script = [assistant("", tool_call("web_search", '{"query": "q"}')),
              assistant("", tool_call("submit_findings", json.dumps(findings))),
              assistant(json.dumps({"subject": "Re", "body": "No thanks.\n{{creator_name}}"}))]
    base = request().model_dump(mode="json")
    base["profile"]["blocked_categories"] = ["alcohol"]
    result, _ = run_full_evaluation(FullEvaluationRequest.model_validate(base), MockProvider(script), search())
    assert result.evaluation.decision == Decision.DECLINE
    assert [h.rule_id for h in result.evaluation.hard_rules] == ["HR-1", "HR-4"]


def test_reject_in_pre_check_skips_agent():
    llm = MockProvider([assistant(json.dumps({"subject": "Re", "body": "Thanks, but no.\n{{creator_name}}"}))])
    srch = search()
    result, trace = run_full_evaluation(request("4_decline_upfront_payment.json"), llm, srch)
    assert result.agent.status == "skipped"
    assert srch.searches == [] and trace == []
    assert result.evaluation.decision == Decision.DECLINE
    assert len(llm.calls) == 1  # 只有邮件


def test_agent_failure_falls_back_to_rules_only():
    llm = MockProvider([LLMError("DeepSeek HTTP 402: Insufficient Balance", status=402), LLMError("x", status=402)])
    result, _ = run_full_evaluation(request(), llm, search())
    assert result.agent.status == "fallback" and "402" in result.agent.error
    assert result.evaluation.brand_research_available is False
    assert result.evaluation.total_score == 68  # 仅规则：35 + 21 + 12
    assert result.email.generated_by == "template"


def test_no_providers_is_rules_only():
    result, _ = run_full_evaluation(request(), None, None, unavailable_reason="DEEPSEEK_API_KEY is not set in .env")
    assert result.agent.status == "unavailable" and "DEEPSEEK_API_KEY" in result.agent.error
    assert result.email.generated_by == "template"
    assert result.agent.usage["cost_cny"] == 0


def test_creator_name_never_sent_to_model():
    llm = MockProvider(agent_script())
    run_full_evaluation(request(creator_name="Maya Secretname"), llm, search())
    sent = json.dumps([[m.content for m in c["messages"]] for c in llm.calls])
    assert "Secretname" not in sent


# ---------- 接口 ----------


def test_evaluate_endpoint_with_mock_providers():
    app.dependency_overrides[get_providers] = lambda: (MockProvider(agent_script()), search(), None)
    try:
        raw = request().model_dump(mode="json")
        response = TestClient(app).post("/api/evaluate", json=raw)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    body = response.json()
    assert body["agent"]["status"] == "completed"
    assert body["evaluation"]["decision"] == "negotiate"
    assert body["email"]["generated_by"] == "llm"


def test_evaluate_endpoint_without_key_is_rules_only():
    app.dependency_overrides[get_providers] = lambda: (None, None, "DEEPSEEK_API_KEY is not set in .env")
    try:
        response = TestClient(app).post("/api/evaluate", json=request().model_dump(mode="json"))
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["agent"]["status"] == "unavailable"
