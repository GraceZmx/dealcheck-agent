"""端到端测试：5 个样例案例的结果与人工计算一致；结果可复现；接口可用。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_pricing_config, load_scoring_config
from app.main import app
from app.models import AdjustmentStatus, Decision, EvaluationRequest
from app.rules.engine import evaluate_deal

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "deals"


def load_example(name: str) -> EvaluationRequest:
    raw = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    raw.pop("_description", None)
    return EvaluationRequest.model_validate(raw)


# 人工计算的预期结果：(文件, 决策, 总分, 报价分, 条款分, 品牌分, 命中规则)
EXPECTED = [
    # 357.5 × 1.25（独家 14 天）= 446.88；400 / 446.88 = 0.895 → 35；条款全绿 35；品牌 12+3+2 = 17
    ("1_accept_fitness_tiktok.json", Decision.ACCEPT, 87, 35, 35, 17, []),
    # 455 × 1.2 = 546；450 / 546 = 0.824 → 35；1 红 1 黄 → 21；品牌 12+1+2 = 15；同类报价调分无来源 → 作废
    ("2_negotiate_maya.json", Decision.NEGOTIATE, 71, 35, 21, 15, ["HR-4"]),
    # 1150 × 1.1 = 1265；1500 / 1265 = 1.19 → 45；35；品牌 12-4 = 8；Agent 有据发现酒精 → HR-1
    ("3_decline_blocked_category.json", Decision.DECLINE, 88, 45, 35, 8, ["HR-1"]),
    # 65 × 1.5 = 97.5；200 / 97.5 = 2.05 → 45；35；仅规则模式品牌 12；要求预付 → HR-3
    ("4_decline_upfront_payment.json", Decision.DECLINE, 92, 45, 35, 12, ["HR-3"]),
    # 20 × 1.0 × 2 = 40；50 / 40 = 1.25 → 45；35；品牌 12-3 = 9；同一网站两篇 → 1 个来源 → HR-6
    ("5_negotiate_single_source.json", Decision.NEGOTIATE, 89, 45, 35, 9, ["HR-6"]),
]


@pytest.mark.parametrize("name, decision, total, pricing, contract, brand, rules", EXPECTED)
def test_example_matches_hand_calculation(name, decision, total, pricing, contract, brand, rules):
    result = evaluate_deal(load_example(name))
    assert result.decision == decision
    assert result.total_score == total
    assert (result.score.pricing.final, result.score.contract.final, result.score.brand_fit.final) == (
        pricing, contract, brand
    )
    assert [h.rule_id for h in result.hard_rules] == rules


def test_all_example_files_are_covered():
    assert {p.name for p in EXAMPLES.glob("*.json")} == {e[0] for e in EXPECTED}


def test_maya_voided_peer_rate_adjustment():
    result = evaluate_deal(load_example("2_negotiate_maya.json"))
    peer = next(a for a in result.score.adjustments if a.item.value == "peer_rate")
    assert (peer.requested, peer.applied, peer.status) == (3, 0, AdjustmentStatus.VOIDED)


def test_reject_reasons_show_hard_rule_message():
    result = evaluate_deal(load_example("3_decline_blocked_category.json"))
    assert result.reasons == [result.hard_rules[0].message]


def test_rules_only_mode_flags_missing_research():
    req = load_example("2_negotiate_maya.json").model_copy(update={"agent_findings": None})
    result = evaluate_deal(req)
    assert result.brand_research_available is False
    assert result.score.brand_fit.final == 12
    assert any("Brand research is not available" in r for r in result.reasons)


@pytest.mark.parametrize("name", [e[0] for e in EXPECTED])
def test_results_are_deterministic(name):
    req = load_example(name)
    first = evaluate_deal(req).model_dump()
    assert all(evaluate_deal(req).model_dump() == first for _ in range(10))


def test_pricing_max_score_matches_scoring_design():
    """总分 100 = 报价 45 + 条款 35 + 品牌 20，两份配置要对得上。"""
    scoring = load_scoring_config()
    assert load_pricing_config().max_score + scoring.contract.max_score + scoring.brand_fit.max_score == 100


def test_evaluate_endpoint():
    raw = json.loads((EXAMPLES / "2_negotiate_maya.json").read_text(encoding="utf-8"))
    raw.pop("_description")
    response = TestClient(app).post("/api/rules/evaluate", json=raw)
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "negotiate"
    assert body["total_score"] == 71


def test_evaluate_endpoint_rejects_unsupported_followers():
    raw = json.loads((EXAMPLES / "2_negotiate_maya.json").read_text(encoding="utf-8"))
    raw.pop("_description")
    raw["profile"]["followers"]["instagram"] = 600_000
    response = TestClient(app).post("/api/rules/evaluate", json=raw)
    assert response.status_code == 422


def test_hr8_caps_high_score_at_negotiate():
    """案例 1 原本 87 分"接受"；加上自由填写的拒接品类命中后，降为"谈判"而不是拒绝。"""
    raw = load_example("1_accept_fitness_tiktok.json").model_dump(mode="json")
    raw["profile"]["blocked_custom"] = ["Caffeinated energy drinks"]
    raw["agent_findings"]["custom_category_matches"] = [
        {"blocked_text": "caffeinated energy drinks", "explanation": "Several products contain 200mg caffeine.",
         "source_ids": ["s3"]}
    ]
    req = EvaluationRequest.model_validate(raw)
    result = evaluate_deal(req)
    assert result.total_score == 87
    assert result.decision == Decision.NEGOTIATE
    assert [h.rule_id for h in result.hard_rules] == ["HR-8"]


def test_hr9_caps_high_score_at_negotiate():
    """价值观明显冲突（values_fit 为 low 且有来源）：87 分也只到"谈判"，不直接拒绝。"""
    raw = load_example("1_accept_fitness_tiktok.json").model_dump(mode="json")
    raw["agent_findings"]["values_fit"] = {"level": "low", "summary": "The brand conflicts with the creator's values.",
                                           "source_ids": ["s3"]}
    result = evaluate_deal(EvaluationRequest.model_validate(raw))
    assert result.decision == Decision.NEGOTIATE
    assert "HR-9" in [h.rule_id for h in result.hard_rules]
