"""打分与决策测试：条款扣分、Agent 调分（作废 / 截断 / 合计上限）、决策阈值和硬性规则优先级。"""

import pytest
from pydantic import ValidationError

from app.config import ScoringConfig, load_scoring_config
from app.models import (
    AdjustmentItem,
    AdjustmentStatus,
    AgentFindings,
    ContractCheckResult,
    Decision,
    HardRuleHit,
    HardRuleKind,
)
from app.rules.scoring import apply_adjustments, compute_score, contract_score, decide


@pytest.fixture(scope="module")
def config() -> ScoringConfig:
    return load_scoring_config()


def contract(red=0, yellow=0) -> ContractCheckResult:
    return ContractCheckResult(terms=[], red_count=red, yellow_count=yellow)


def findings(*adjustments) -> AgentFindings:
    return AgentFindings(
        brand_entity_confirmed=True,
        score_adjustments=[
            {"item": item, "delta": delta, "reason": "test", "source_ids": src}
            for item, delta, src in adjustments
        ],
        sources=[{"id": "s1", "url": "https://a.example/1"}, {"id": "s2", "url": "https://b.example/2"}],
    )


REJECT_HIT = HardRuleHit(rule_id="HR-1", kind=HardRuleKind.REJECT, message="x")
CAP_HIT = HardRuleHit(rule_id="HR-4", kind=HardRuleKind.CAP, message="x")


# ---------- 合同条款分 ----------


@pytest.mark.parametrize(
    "red, yellow, expected",
    [(0, 0, 35), (0, 1, 31), (1, 0, 25), (1, 1, 21), (2, 1, 11), (3, 1, 1), (4, 0, 0), (4, 4, 0)],
)
def test_contract_score(config, red, yellow, expected):
    assert contract_score(contract(red, yellow), config) == expected


# ---------- Agent 调分 ----------


def test_no_findings_means_no_adjustments(config):
    assert apply_adjustments(None, config) == []


def test_adjustment_with_source_applied(config):
    [a] = apply_adjustments(findings(("brand_reputation", 3, ["s1"])), config)
    assert (a.applied, a.status) == (3, AdjustmentStatus.APPLIED)
    assert a.source_urls == ["https://a.example/1"]


@pytest.mark.parametrize("source_ids", [[], ["missing"]])
def test_adjustment_without_valid_source_voided(config, source_ids):
    [a] = apply_adjustments(findings(("brand_reputation", 3, source_ids)), config)
    assert (a.requested, a.applied, a.status) == (3, 0, AdjustmentStatus.VOIDED)


@pytest.mark.parametrize(
    "item, delta, applied",
    [
        ("brand_reputation", -8, -8),
        ("brand_reputation", -9, -8),
        ("brand_reputation", 4, 4),
        ("brand_reputation", 5, 4),
        ("values_fit", -6, -6),
        ("values_fit", -7, -6),
        ("values_fit", 5, 4),
        ("peer_rate", -6, -5),
        ("peer_rate", 6, 5),
    ],
)
def test_adjustment_range_clamping(config, item, delta, applied):
    [a] = apply_adjustments(findings((item, delta, ["s1"])), config)
    assert a.applied == applied
    expected_status = AdjustmentStatus.APPLIED if applied == delta else AdjustmentStatus.CLAMPED
    assert a.status == expected_status


def test_same_item_adjustments_summed_then_clamped(config):
    [a] = apply_adjustments(findings(("brand_reputation", 3, ["s1"]), ("brand_reputation", 3, ["s2"])), config)
    assert (a.requested, a.applied, a.status) == (6, 4, AdjustmentStatus.CLAMPED)


def test_partially_sourced_item_only_counts_valid_part(config):
    [a] = apply_adjustments(findings(("values_fit", 2, ["s1"]), ("values_fit", 2, [])), config)
    assert (a.requested, a.applied) == (4, 2)
    assert "voided" in a.reason


# ---------- 维度分与合计上限 ----------


def test_rules_only_score(config):
    s = compute_score(35, 45, contract(1, 1), None, config)
    assert (s.pricing.final, s.contract.final, s.brand_fit.final, s.total) == (35, 21, 12, 68)


def test_brand_fit_adjustments(config):
    s = compute_score(35, 45, contract(), findings(("brand_reputation", 3, ["s1"]), ("values_fit", 2, ["s2"])), config)
    assert s.brand_fit.final == 17
    assert s.total == 35 + 35 + 17


def test_peer_rate_adjusts_pricing_within_bounds(config):
    up = compute_score(43, 45, contract(), findings(("peer_rate", 5, ["s1"])), config)
    assert up.pricing.final == 45 and up.pricing.adjustment == 2  # 不超过满分 45
    down = compute_score(3, 45, contract(), findings(("peer_rate", -5, ["s1"])), config)
    assert down.pricing.final == 0 and down.pricing.adjustment == -3  # 不低于 0


def test_total_adjustment_capped_at_15(config):
    """口碑 -8、价值观 -6 → 品牌分 12-14 → 截到 0（实际 -12）；同类报价 -5 → 合计 -17 → 截到 -15。"""
    f = findings(("brand_reputation", -8, ["s1"]), ("values_fit", -6, ["s1"]), ("peer_rate", -5, ["s2"]))
    s = compute_score(30, 45, contract(), f, config)
    assert s.brand_fit.final == 0
    assert s.pricing.final == 25
    assert s.total_cap_correction == 2
    assert s.total == 30 + 35 + 12 - 15


def test_positive_adjustments_below_cap_not_corrected(config):
    f = findings(("brand_reputation", 4, ["s1"]), ("values_fit", 4, ["s1"]), ("peer_rate", 5, ["s2"]))
    s = compute_score(30, 45, contract(), f, config)
    assert s.total_cap_correction == 0
    assert s.total == 30 + 35 + 12 + 13


def test_total_cap_comes_from_config(config):
    tight = config.model_copy(deep=True)
    tight.adjustments.total_cap = 3
    s = compute_score(30, 45, contract(), findings(("brand_reputation", 4, ["s1"])), tight)
    assert s.total == 30 + 35 + 12 + 3


# ---------- 决策 ----------


@pytest.mark.parametrize(
    "total, expected",
    [(100, Decision.ACCEPT), (75, Decision.ACCEPT), (74, Decision.NEGOTIATE),
     (50, Decision.NEGOTIATE), (49, Decision.DECLINE), (0, Decision.DECLINE)],
)
def test_decision_thresholds(config, total, expected):
    assert decide(total, [], config) == expected


@pytest.mark.parametrize("total", [100, 75])
def test_cap_rule_downgrades_accept_to_negotiate(config, total):
    assert decide(total, [CAP_HIT], config) == Decision.NEGOTIATE


@pytest.mark.parametrize("total, expected", [(60, Decision.NEGOTIATE), (40, Decision.DECLINE)])
def test_cap_rule_does_not_change_lower_decisions(config, total, expected):
    assert decide(total, [CAP_HIT], config) == expected


@pytest.mark.parametrize("total", [100, 75, 60, 0])
def test_reject_rule_always_declines(config, total):
    assert decide(total, [REJECT_HIT, CAP_HIT], config) == Decision.DECLINE


def test_thresholds_come_from_config(config):
    strict = config.model_copy(deep=True)
    strict.decision.accept_min_score = 80
    assert decide(78, [], strict) == Decision.NEGOTIATE


def test_config_rejects_inverted_thresholds(config):
    raw = config.model_dump()
    raw["decision"] = {"accept_min_score": 50, "negotiate_min_score": 75}
    with pytest.raises(ValidationError):
        ScoringConfig.model_validate(raw)


def test_adjustment_items_all_have_ranges(config):
    for item in AdjustmentItem:
        rng = config.adjustments.range_for(item)
        assert rng.min <= 0 <= rng.max
