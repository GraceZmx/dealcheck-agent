"""合同条款红黄绿灯检查（PRD 5.5 节）。

阈值全部来自 contract_rules.yaml；提示文案引用配置中的数字，保证文案和判定一致。
"""

from app.config import ContractRulesConfig, load_contract_rules
from app.models import (
    ContractCheckResult,
    ContractTerms,
    Exclusivity,
    ExclusivityScope,
    Light,
    TermName,
    TermResult,
    UsageRights,
    UsageType,
)


def check_exclusivity(ex: Exclusivity, rules: ContractRulesConfig) -> TermResult:
    r = rules.exclusivity
    term = TermName.EXCLUSIVITY

    if ex.scope == ExclusivityScope.NONE or ex.days == 0:
        return TermResult(term=term, light=Light.GREEN)

    if ex.scope == ExclusivityScope.INDUSTRY:
        if ex.days <= r.industry_yellow_max_days:
            return TermResult(
                term=term,
                light=Light.YELLOW,
                risk=f"Industry-wide exclusivity blocks all other brand deals for {ex.days} days, not just competitors.",
                suggestion="Ask to limit exclusivity to direct competitors in the same product category.",
            )
        return TermResult(
            term=term,
            light=Light.RED,
            risk=f"Industry-wide exclusivity for {ex.days} days (over {r.industry_yellow_max_days}) blocks all other brand income for a long period.",
            suggestion=f"Limit exclusivity to direct competitors, for no more than {r.category_green_max_days} days, or ask for an exclusivity fee.",
        )

    # 同品类竞品独家
    if ex.days <= r.category_green_max_days:
        return TermResult(term=term, light=Light.GREEN)
    if ex.days <= r.category_yellow_max_days:
        return TermResult(
            term=term,
            light=Light.YELLOW,
            risk=f"Competitor exclusivity of {ex.days} days is longer than the typical {r.category_green_max_days} days.",
            suggestion=f"Ask to shorten exclusivity to {r.category_green_max_days} days, or add an exclusivity fee for the extra time.",
        )
    return TermResult(
        term=term,
        light=Light.RED,
        risk=f"Competitor exclusivity of {ex.days} days (over {r.category_yellow_max_days}) can cost you other deals in your niche.",
        suggestion=f"Ask to cap exclusivity at {r.category_green_max_days} days, or charge a separate exclusivity fee.",
    )


def check_usage_rights(ur: UsageRights, rules: ContractRulesConfig) -> TermResult:
    r = rules.usage_rights
    term = TermName.USAGE_RIGHTS
    fix = f"Request usage rights limited to {r.paid_green_max_days} days of paid usage, or an additional usage fee."

    if ur.perpetual:
        return TermResult(
            term=term,
            light=Light.RED,
            risk="Perpetual usage rights let the brand use your content in ads forever without paying you again.",
            suggestion=fix,
        )

    if ur.type == UsageType.ORGANIC:
        return TermResult(term=term, light=Light.GREEN)

    if ur.duration_days is None:
        return TermResult(
            term=term,
            light=Light.RED,
            risk="Paid usage has no stated end date, which could be read as unlimited use of your content in ads.",
            suggestion=fix,
        )

    if ur.duration_days <= r.paid_green_max_days:
        return TermResult(term=term, light=Light.GREEN)

    if ur.duration_days <= r.paid_yellow_max_days:
        return TermResult(
            term=term,
            light=Light.YELLOW,
            risk=f"Paid usage for {ur.duration_days} days goes beyond the typical {r.paid_green_max_days} days.",
            suggestion=fix,
        )

    if ur.paid_usage_billed_separately:
        return TermResult(
            term=term,
            light=Light.YELLOW,
            risk=f"Paid usage for {ur.duration_days} days is long, even though it is billed separately.",
            suggestion="Confirm the usage fee reflects the full duration, or shorten the usage period.",
        )
    return TermResult(
        term=term,
        light=Light.RED,
        risk=f"Paid usage for {ur.duration_days} days (over {r.paid_yellow_max_days}) with no extra fee means the brand gets long-term ads for free.",
        suggestion=fix,
    )


def check_kill_fee(kill_fee_percent: float | None, rules: ContractRulesConfig) -> TermResult:
    r = rules.kill_fee
    term = TermName.KILL_FEE
    fix = f"Ask for a kill fee of at least {r.green_min_percent:g}% if the brand cancels after you start work."

    if kill_fee_percent is None:
        return TermResult(
            term=term,
            light=Light.RED,
            risk="The contract has no kill fee, so the brand can cancel after you've done the work and pay nothing.",
            suggestion=fix,
        )
    if kill_fee_percent >= r.green_min_percent:
        return TermResult(term=term, light=Light.GREEN)
    if kill_fee_percent >= r.yellow_min_percent:
        return TermResult(
            term=term,
            light=Light.YELLOW,
            risk=f"A {kill_fee_percent:g}% kill fee only partly covers your time if the brand cancels.",
            suggestion=fix,
        )
    return TermResult(
        term=term,
        light=Light.RED,
        risk=f"A {kill_fee_percent:g}% kill fee (under {r.yellow_min_percent:g}%) leaves you mostly unpaid if the brand cancels.",
        suggestion=fix,
    )


def check_revisions(revisions: int | str, rules: ContractRulesConfig) -> TermResult:
    r = rules.revisions
    term = TermName.REVISIONS
    fix = f"Limit revisions to {r.green_max} rounds, with extra rounds billed separately."

    if revisions == "unlimited":
        return TermResult(
            term=term,
            light=Light.RED,
            risk="Unlimited revisions let the brand ask for changes indefinitely, with no cap on your time.",
            suggestion=fix,
        )
    if revisions <= r.green_max:
        return TermResult(term=term, light=Light.GREEN)
    if revisions <= r.yellow_max:
        return TermResult(
            term=term,
            light=Light.YELLOW,
            risk=f"{revisions} revision rounds is more than the typical {r.green_max}.",
            suggestion=fix,
        )
    return TermResult(
        term=term,
        light=Light.RED,
        risk=f"{revisions} revision rounds (over {r.yellow_max}) can take up much more time than the fee covers.",
        suggestion=fix,
    )


def check_contract(terms: ContractTerms, rules: ContractRulesConfig | None = None) -> ContractCheckResult:
    """检查全部四类条款。rules 为空时从配置文件读取。"""
    rules = rules or load_contract_rules()
    results = [
        check_exclusivity(terms.exclusivity, rules),
        check_usage_rights(terms.usage_rights, rules),
        check_kill_fee(terms.kill_fee_percent, rules),
        check_revisions(terms.revisions, rules),
    ]
    return ContractCheckResult(
        terms=results,
        red_count=sum(t.light == Light.RED for t in results),
        yellow_count=sum(t.light == Light.YELLOW for t in results),
    )
