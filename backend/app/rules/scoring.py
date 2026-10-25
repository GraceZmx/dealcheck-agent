"""打分与决策（PRD 第 7 节）。

计算顺序：
1. 每项 Agent 调分：没有有效来源 → 作废；超出单项范围 → 截断；同一项多次调分先相加再截断
2. 维度分：报价 = 报价分 + 同类报价调分（0–45）；品牌与匹配 = 默认分 + 口碑 + 价值观调分（0–20）
3. 调分合计（按维度上下限修正后）超过 ±total_cap 时截断
4. 决策：拒绝类规则优先 → 分数阈值 → 封顶类规则把"接受"降为"谈判"
"""

from app.config import ScoringConfig, load_scoring_config
from app.models import (
    AdjustmentItem,
    AdjustmentStatus,
    AgentFindings,
    AppliedAdjustment,
    ContractCheckResult,
    Decision,
    DimensionScore,
    HardRuleHit,
    HardRuleKind,
    ScoreBreakdown,
)
from app.rules.evidence import resolve_urls


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def contract_score(contract: ContractCheckResult, config: ScoringConfig) -> int:
    c = config.contract
    deduction = contract.red_count * c.red_deduction + contract.yellow_count * c.yellow_deduction
    return max(0, c.max_score - deduction)


def apply_adjustments(findings: AgentFindings | None, config: ScoringConfig) -> list[AppliedAdjustment]:
    """校验每项调分。同一项的多次调分合并为一条：先相加，再截断到允许范围。"""
    if not findings:
        return []

    merged: dict[AdjustmentItem, dict] = {}
    for adj in findings.score_adjustments:
        urls = resolve_urls(adj.source_ids, findings)
        entry = merged.setdefault(adj.item, {"requested": 0, "valid": 0, "reasons": [], "urls": [], "voided": False})
        entry["requested"] += adj.delta
        if urls:
            entry["valid"] += adj.delta
            entry["reasons"].append(adj.reason)
            entry["urls"] += [u for u in urls if u not in entry["urls"]]
        else:
            entry["voided"] = True
            entry["reasons"].append(f"[voided: no valid source] {adj.reason}")

    results = []
    for item in AdjustmentItem:
        if item not in merged:
            continue
        e = merged[item]
        rng = config.adjustments.range_for(item)
        applied = _clamp(e["valid"], rng.min, rng.max)
        if not e["urls"]:
            status = AdjustmentStatus.VOIDED
        elif applied != e["valid"]:
            status = AdjustmentStatus.CLAMPED
        else:
            status = AdjustmentStatus.APPLIED
        results.append(AppliedAdjustment(
            item=item,
            requested=e["requested"],
            applied=applied,
            status=status,
            reason=" / ".join(e["reasons"]),
            source_urls=e["urls"],
        ))
    return results


def compute_score(
    pricing_score: int,
    pricing_max: int,
    contract: ContractCheckResult,
    findings: AgentFindings | None,
    config: ScoringConfig | None = None,
) -> ScoreBreakdown:
    config = config or load_scoring_config()
    adjustments = apply_adjustments(findings, config)
    delta = {a.item: a.applied for a in adjustments}

    pricing_final = _clamp(pricing_score + delta.get(AdjustmentItem.PEER_RATE, 0), 0, pricing_max)

    bf = config.brand_fit
    brand_delta = delta.get(AdjustmentItem.BRAND_REPUTATION, 0) + delta.get(AdjustmentItem.VALUES_FIT, 0)
    brand_final = _clamp(bf.default_score + brand_delta, 0, bf.max_score)

    c_score = contract_score(contract, config)

    effective_delta = (pricing_final - pricing_score) + (brand_final - bf.default_score)
    cap = config.adjustments.total_cap
    capped_delta = _clamp(effective_delta, -cap, cap)

    return ScoreBreakdown(
        pricing=DimensionScore(base=pricing_score, adjustment=pricing_final - pricing_score, final=pricing_final, max=pricing_max),
        contract=DimensionScore(base=c_score, adjustment=0, final=c_score, max=config.contract.max_score),
        brand_fit=DimensionScore(base=bf.default_score, adjustment=brand_final - bf.default_score, final=brand_final, max=bf.max_score),
        adjustments=adjustments,
        total_cap_correction=capped_delta - effective_delta,
        total=pricing_score + c_score + bf.default_score + capped_delta,
    )


def decide(total: int, hard_rules: list[HardRuleHit], config: ScoringConfig | None = None) -> Decision:
    config = config or load_scoring_config()
    t = config.decision
    if any(h.kind == HardRuleKind.REJECT for h in hard_rules):
        return Decision.DECLINE
    if total >= t.accept_min_score:
        if any(h.kind == HardRuleKind.CAP for h in hard_rules):
            return Decision.NEGOTIATE
        return Decision.ACCEPT
    if total >= t.negotiate_min_score:
        return Decision.NEGOTIATE
    return Decision.DECLINE
