"""硬性规则（PRD 8.1 节）。

拒绝类（命中即建议拒绝，分数和 Agent 都推翻不了）：
    HR-1 品牌涉及创作者拒接品类    HR-2 严重负面（≥ 2 个独立来源）    HR-3 要求预付费用
封顶类（命中后最多到"谈判"）：
    HR-4 有红灯条款    HR-5 报价低于最低接单价    HR-6 严重负面只有单一来源    HR-7 无法确认品牌实体
    HR-8 品牌可能涉及创作者自由填写的拒接品类（Agent 语义判断，需用户自行核实）

HR-2 / HR-6 / HR-7 / HR-8 的证据来自 Agent，判定逻辑在这里，由规则决定是否命中。
"""

from app.config import HardRulesConfig, load_hard_rules_config
from app.models import (
    AgentFindings,
    ContractCheckResult,
    CreatorProfile,
    DealInput,
    HardRuleHit,
    HardRuleKind,
    Light,
)
from app.rules.evidence import independent_source_count, resolve_urls

REJECT, CAP = HardRuleKind.REJECT, HardRuleKind.CAP


def check_blocked_categories(
    profile: CreatorProfile,
    deal: DealInput,
    findings: AgentFindings | None,
    config: HardRulesConfig,
) -> list[HardRuleHit]:
    """HR-1：表单勾选的品类，或 Agent 有据发现的品类，命中拒接清单。"""
    blocked = set(profile.blocked_categories)
    hits = []

    for category in sorted(blocked & set(deal.brand_categories), key=lambda c: c.value):
        hits.append(HardRuleHit(
            rule_id="HR-1", kind=REJECT,
            message=f"The brand is in a category you don't work with: {category.value}.",
        ))

    if findings:
        reported = {h.message for h in hits}
        for detected in findings.detected_categories:
            if detected.category not in blocked:
                continue
            urls = resolve_urls(detected.source_ids, findings)
            if len(urls) < config.detected_category_min_sources:
                continue
            message = f"Research shows the brand is involved in a category you don't work with: {detected.category.value}."
            if message not in reported:
                hits.append(HardRuleHit(rule_id="HR-1", kind=REJECT, message=message, evidence_urls=urls))
                reported.add(message)
    return hits


def check_severe_negatives(findings: AgentFindings | None, config: HardRulesConfig) -> list[HardRuleHit]:
    """HR-2 / HR-6：同一条严重负面需要足够多的独立来源才拒绝，否则封顶为谈判。"""
    if not findings:
        return []
    hits = []
    for flag in findings.severe_flags:
        urls = resolve_urls(flag.source_ids, findings)
        n = independent_source_count(urls)
        if n >= config.severe_negative_min_independent_sources:
            hits.append(HardRuleHit(
                rule_id="HR-2", kind=REJECT,
                message=f"{n} independent sources report a serious issue ({flag.type.value}): {flag.description}",
                evidence_urls=urls,
            ))
        elif n >= 1:
            hits.append(HardRuleHit(
                rule_id="HR-6", kind=CAP,
                message=f"One source reports a serious issue ({flag.type.value}) that needs verification: {flag.description}",
                evidence_urls=urls,
            ))
    return hits


def check_upfront_payment(deal: DealInput) -> list[HardRuleHit]:
    """HR-3：要求创作者预付费用是典型的诈骗信号。"""
    if deal.requires_upfront_payment:
        return [HardRuleHit(
            rule_id="HR-3", kind=REJECT,
            message="The brand asks you to pay upfront. Legitimate brands don't charge creators to work with them.",
        )]
    return []


def check_red_terms(contract: ContractCheckResult) -> list[HardRuleHit]:
    """HR-4：任一条款为红灯。"""
    red = [t.term.value for t in contract.terms if t.light == Light.RED]
    if red:
        return [HardRuleHit(
            rule_id="HR-4", kind=CAP,
            message=f"High-risk contract terms need to be changed first: {', '.join(red)}.",
        )]
    return []


def check_rate_floor(profile: CreatorProfile, deal: DealInput) -> list[HardRuleHit]:
    """HR-5：报价低于创作者设定的最低接单价。"""
    if profile.rate_floor_usd is not None and deal.offer_usd < profile.rate_floor_usd:
        return [HardRuleHit(
            rule_id="HR-5", kind=CAP,
            message=f"The offer (${deal.offer_usd:,.0f}) is below your minimum rate (${profile.rate_floor_usd:,.0f}).",
        )]
    return []


def check_brand_entity(findings: AgentFindings | None) -> list[HardRuleHit]:
    """HR-7：Agent 无法确认品牌实体。仅规则模式（没有 Agent 结果）不触发。"""
    if findings is not None and not findings.brand_entity_confirmed:
        return [HardRuleHit(
            rule_id="HR-7", kind=CAP,
            message="We couldn't confirm this brand's identity from public information. Ask for company details before agreeing.",
        )]
    return []


def check_custom_categories(
    profile: CreatorProfile, findings: AgentFindings | None, config: HardRulesConfig
) -> list[HardRuleHit]:
    """HR-8：Agent 判断品牌和创作者自由填写的拒接品类相符。

    只认创作者档案里确实写了的条目（忽略大小写和首尾空格），防止 Agent 自己编一个拒接理由。
    """
    if not findings or not profile.blocked_custom:
        return []
    custom = {c.casefold(): c for c in profile.blocked_custom}
    hits, reported = [], set()
    for match in findings.custom_category_matches:
        original = custom.get(match.blocked_text.strip().casefold())
        if original is None or original in reported:
            continue
        urls = resolve_urls(match.source_ids, findings)
        if len(urls) < config.custom_category_min_sources:
            continue
        hits.append(HardRuleHit(
            rule_id="HR-8", kind=CAP,
            message=f'This brand may relate to something you don\'t work with ("{original}"): '
                    f"{match.explanation} This is an automated judgment, so please verify it yourself.",
            evidence_urls=urls,
        ))
        reported.add(original)
    return hits


def evaluate_hard_rules(
    profile: CreatorProfile,
    deal: DealInput,
    contract: ContractCheckResult,
    findings: AgentFindings | None = None,
    config: HardRulesConfig | None = None,
) -> list[HardRuleHit]:
    """检查全部硬性规则，按规则编号顺序返回命中项。"""
    config = config or load_hard_rules_config()
    hits = (
        check_blocked_categories(profile, deal, findings, config)
        + check_severe_negatives(findings, config)
        + check_upfront_payment(deal)
        + check_red_terms(contract)
        + check_rate_floor(profile, deal)
        + check_brand_entity(findings)
        + check_custom_categories(profile, findings, config)
    )
    return sorted(hits, key=lambda h: int(h.rule_id.split("-")[1]))
