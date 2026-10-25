"""报价评估（PRD 5.4 节）：查基准表 → 乘领域系数和修正系数 → 报价比 → 得分与建议还价区间。

金额保留 2 位小数，报价比保留 4 位小数后再分档，避免浮点误差让边界值落错档。
"""

from app.config import BenchmarkData, PricingConfig, load_benchmarks, load_pricing_config
from app.models import (
    Adjustments,
    Confidence,
    CounterRange,
    Exclusivity,
    ExclusivityScope,
    LineItem,
    OfferPosition,
    PriceRange,
    PricingInput,
    PricingResult,
    Tier,
    UsageRights,
    UsageType,
)

LOW_CONFIDENCE_MESSAGE = (
    "This benchmark is a rough estimate based on limited public data. "
    "Use it as a reference point, not a firm market rate."
)


class UnsupportedFollowerCountError(ValueError):
    """粉丝数不在支持的量级范围内。"""


def get_tier(followers: int, config: PricingConfig) -> Tier:
    """按粉丝数判定量级（含下限、不含上限）。"""
    for t in config.tiers:
        if t.min_followers <= followers < t.max_followers:
            return t.name
    lo, hi = config.tiers[0].min_followers, config.tiers[-1].max_followers
    raise UnsupportedFollowerCountError(
        f"粉丝数 {followers:,} 不在支持范围内（{lo:,} ≤ 粉丝数 < {hi:,}）"
    )


def usage_rights_uplift(ur: UsageRights, config: PricingConfig) -> float:
    """付费使用没有单独计费时，基准价要加上使用权溢价。"""
    cfg = config.adjustments.usage_rights
    if ur.paid_usage_billed_separately:
        return 0.0
    if ur.perpetual:
        return cfg.long_or_perpetual_not_billed_uplift
    if ur.type == UsageType.ORGANIC:
        return 0.0
    if ur.duration_days is None or ur.duration_days > cfg.long_threshold_days:
        return cfg.long_or_perpetual_not_billed_uplift
    return cfg.paid_not_billed_uplift


def exclusivity_uplift(ex: Exclusivity, config: PricingConfig) -> float:
    if ex.scope == ExclusivityScope.NONE or ex.days == 0:
        return 0.0
    for band in config.adjustments.exclusivity:
        if band.max_days is None or ex.days <= band.max_days:
            return band.uplift
    raise AssertionError("最后一档 max_days 为空，不会走到这里")


def engagement_factor(config: PricingConfig) -> float:
    if config.adjustments.engagement.enabled:
        raise NotImplementedError("互动率修正在 V0 中尚未实现，请在 pricing.yaml 中保持 enabled: false")
    return 1.0


def score_for_ratio(ratio: float, config: PricingConfig) -> int:
    for band in config.score_bands:
        if ratio >= band.min_ratio:
            return band.points
    raise AssertionError("最后一档 min_ratio 为 0，不会走到这里")


def offer_position(offer: float, bench: PriceRange) -> OfferPosition:
    if offer < bench.p25:
        return OfferPosition.BELOW_P25
    if offer < bench.p50:
        return OfferPosition.P25_TO_P50
    if offer <= bench.p75:
        return OfferPosition.P50_TO_P75
    return OfferPosition.ABOVE_P75


def _scale(r: PriceRange, k: float) -> PriceRange:
    return PriceRange(p25=round(r.p25 * k, 2), p50=round(r.p50 * k, 2), p75=round(r.p75 * k, 2))


def evaluate_pricing(
    inp: PricingInput,
    config: PricingConfig | None = None,
    data: BenchmarkData | None = None,
) -> PricingResult:
    """评估报价是否合理。config / data 为空时从文件读取。"""
    config = config or load_pricing_config()
    data = data or load_benchmarks()

    niche = data.niche_coefficients[inp.niche]
    notes: list[str] = []

    line_items = []
    for d in inp.deliverables:
        tier = get_tier(inp.followers[d.type.platform], config)
        row = data.base_price(d.type, tier)
        base = PriceRange(p25=row.p25, p50=row.p50, p75=row.p75)
        line_items.append(
            LineItem(
                type=d.type,
                quantity=d.quantity,
                tier=tier,
                base_unit_price=base,
                unit_price=_scale(base, niche.coefficient),
                confidence=row.confidence,
            )
        )
        if row.confidence == Confidence.LOW:
            notes.append(f"{d.type.value} / {tier.value}: {row.method}")

    if niche.confidence == Confidence.LOW:
        notes.append(f"Niche coefficient for {inp.niche.value}: {niche.note}")

    usage = usage_rights_uplift(inp.usage_rights, config)
    excl = exclusivity_uplift(inp.exclusivity, config)
    engagement = engagement_factor(config)
    if usage:
        notes.append(f"Usage rights uplift (+{usage:.0%}) comes from a single public source.")
    if excl:
        notes.append(f"Exclusivity uplift (+{excl:.0%}) comes from a single public source.")

    multiplier = (1 + usage) * (1 + excl) * engagement
    niche_total = PriceRange(
        p25=sum(li.unit_price.p25 * li.quantity for li in line_items),
        p50=sum(li.unit_price.p50 * li.quantity for li in line_items),
        p75=sum(li.unit_price.p75 * li.quantity for li in line_items),
    )
    bench = _scale(niche_total, multiplier)

    ratio = round(inp.offer_usd / bench.p50, 4)
    counter = None
    if inp.offer_usd < bench.p50:
        counter = CounterRange(low=bench.p50, high=bench.p75)

    confidence = Confidence.LOW if notes else Confidence.MEDIUM
    return PricingResult(
        line_items=line_items,
        adjustments=Adjustments(
            niche_coefficient=niche.coefficient,
            usage_rights_uplift=usage,
            exclusivity_uplift=excl,
            engagement_factor=engagement,
            total_multiplier=round(multiplier, 4),
        ),
        adjusted_benchmark=bench,
        offer_usd=inp.offer_usd,
        offer_ratio=ratio,
        position=offer_position(inp.offer_usd, bench),
        score=score_for_ratio(ratio, config),
        max_score=config.max_score,
        suggested_counter_range=counter,
        confidence=confidence,
        confidence_notes=notes,
        confidence_message=LOW_CONFIDENCE_MESSAGE if confidence == Confidence.LOW else None,
        data_version=data.version,
        data_date=data.data_date,
    )
