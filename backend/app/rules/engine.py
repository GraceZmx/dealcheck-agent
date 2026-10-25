"""规则引擎总调度：条款检查 → 报价评估 → 硬性规则 → 打分 → 决策。"""

from app.config import (
    BenchmarkData,
    ContractRulesConfig,
    HardRulesConfig,
    PricingConfig,
    ScoringConfig,
    load_benchmarks,
    load_contract_rules,
    load_hard_rules_config,
    load_pricing_config,
    load_scoring_config,
)
from app.models import (
    Decision,
    EvaluationRequest,
    EvaluationResult,
    HardRuleKind,
    PricingInput,
)
from app.rules.contract import check_contract
from app.rules.hard_rules import evaluate_hard_rules
from app.rules.pricing import evaluate_pricing
from app.rules.scoring import compute_score, decide


def evaluate_deal(
    req: EvaluationRequest,
    contract_rules: ContractRulesConfig | None = None,
    pricing_config: PricingConfig | None = None,
    benchmarks: BenchmarkData | None = None,
    hard_rules_config: HardRulesConfig | None = None,
    scoring_config: ScoringConfig | None = None,
) -> EvaluationResult:
    """评估一个合作邀请。配置为空时从文件读取。"""
    pricing_config = pricing_config or load_pricing_config()
    scoring_config = scoring_config or load_scoring_config()
    profile, deal, findings = req.profile, req.deal, req.agent_findings

    contract = check_contract(deal.contract, contract_rules or load_contract_rules())
    pricing = evaluate_pricing(
        PricingInput(
            followers=profile.followers,
            niche=profile.niche,
            deliverables=deal.deliverables,
            offer_usd=deal.offer_usd,
            usage_rights=deal.contract.usage_rights,
            exclusivity=deal.contract.exclusivity,
        ),
        pricing_config,
        benchmarks or load_benchmarks(),
    )
    hits = evaluate_hard_rules(profile, deal, contract, findings, hard_rules_config or load_hard_rules_config())
    score = compute_score(pricing.score, pricing.max_score, contract, findings, scoring_config)
    decision = decide(score.total, hits, scoring_config)

    return EvaluationResult(
        decision=decision,
        total_score=score.total,
        reasons=_reasons(decision, score.total, pricing.offer_ratio, contract, hits, findings is not None),
        hard_rules=hits,
        score=score,
        pricing=pricing,
        contract=contract,
        brand_research_available=findings is not None,
    )


def _reasons(decision, total, ratio, contract, hits, has_research) -> list[str]:
    """生成给用户看的核心理由（英文，面向海外创作者）。"""
    rejects = [h.message for h in hits if h.kind == HardRuleKind.REJECT]
    if rejects:
        return rejects

    reasons = [f"Overall score: {total}/100."]
    reasons.append(f"The offer is {ratio:.0%} of the typical rate for your tier and niche.")
    if contract.red_count or contract.yellow_count:
        reasons.append(
            f"The contract has {contract.red_count} high-risk and {contract.yellow_count} medium-risk terms."
        )
    else:
        reasons.append("The contract terms are within typical ranges.")
    caps = [h.message for h in hits if h.kind == HardRuleKind.CAP]
    if decision == Decision.NEGOTIATE and caps:
        reasons += caps
    if not has_research:
        reasons.append("Brand research is not available, so the brand score is neutral.")
    return reasons
