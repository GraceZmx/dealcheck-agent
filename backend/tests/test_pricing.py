"""报价评估测试：量级边界、报价分档边界、修正系数、多项交付、置信度。"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import PricingConfig, load_benchmarks, load_pricing_config
from app.main import app
from app.models import (
    Confidence,
    Exclusivity,
    ExclusivityScope,
    OfferPosition,
    PricingInput,
    Tier,
    UsageRights,
    UsageType,
)
from app.rules.pricing import (
    UnsupportedFollowerCountError,
    evaluate_pricing,
    exclusivity_uplift,
    get_tier,
    score_for_ratio,
    usage_rights_uplift,
)


@pytest.fixture(scope="module")
def config() -> PricingConfig:
    return load_pricing_config()


@pytest.fixture(scope="module")
def data():
    return load_benchmarks()


def _maya(**overrides) -> PricingInput:
    """Micro 级美妆博主，Instagram 28K 粉丝，品牌出价 $300 买一条 Reel。"""
    fields = dict(
        followers={"instagram": 28_000},
        niche="beauty",
        deliverables=[{"type": "instagram_reel"}],
        offer_usd=300,
    )
    fields.update(overrides)
    return PricingInput(**fields)


# ---------- 量级判定 ----------


@pytest.mark.parametrize(
    "followers, tier",
    [
        (1_000, Tier.NANO),
        (9_999, Tier.NANO),
        (10_000, Tier.MICRO),
        (49_999, Tier.MICRO),
        (50_000, Tier.MID),
        (99_999, Tier.MID),
        (100_000, Tier.MID_PLUS),
        (249_999, Tier.MID_PLUS),
        (250_000, Tier.UPPER),
        (499_999, Tier.UPPER),
    ],
)
def test_tier_boundaries(config, followers, tier):
    assert get_tier(followers, config) == tier


@pytest.mark.parametrize("followers", [0, 999, 500_000, 2_000_000])
def test_followers_out_of_range_are_unsupported(config, followers):
    with pytest.raises(UnsupportedFollowerCountError):
        get_tier(followers, config)


# ---------- 报价分档 ----------


@pytest.mark.parametrize(
    "ratio, points",
    [
        (2.0, 45),
        (1.0, 45),
        (0.9999, 35),
        (0.8, 35),
        (0.7999, 22),
        (0.6, 22),
        (0.5999, 10),
        (0.4, 10),
        (0.3999, 0),
        (0.0, 0),
    ],
)
def test_score_band_boundaries(config, ratio, points):
    assert score_for_ratio(ratio, config) == points


def test_max_score_is_45(config):
    assert config.max_score == 45


# ---------- 修正系数 ----------


@pytest.mark.parametrize(
    "usage, expected",
    [
        (UsageRights(type=UsageType.ORGANIC), 0.0),
        (UsageRights(type=UsageType.PAID_ADS, duration_days=30), 0.20),
        (UsageRights(type=UsageType.PAID_ADS, duration_days=180), 0.20),
        (UsageRights(type=UsageType.PAID_ADS, duration_days=181), 0.50),
        (UsageRights(type=UsageType.WHITELISTING, duration_days=90), 0.20),
        (UsageRights(type=UsageType.PAID_ADS, duration_days=None), 0.50),
        (UsageRights(type=UsageType.PAID_ADS, perpetual=True), 0.50),
        (UsageRights(type=UsageType.ORGANIC, perpetual=True), 0.50),
        # 单独计费时使用费不在报价里，基准不加价
        (UsageRights(type=UsageType.PAID_ADS, duration_days=90, paid_usage_billed_separately=True), 0.0),
        (UsageRights(type=UsageType.PAID_ADS, perpetual=True, paid_usage_billed_separately=True), 0.0),
    ],
)
def test_usage_rights_uplift(config, usage, expected):
    assert usage_rights_uplift(usage, config) == expected


@pytest.mark.parametrize(
    "days, scope, expected",
    [
        (0, ExclusivityScope.CATEGORY, 0.0),
        (60, ExclusivityScope.NONE, 0.0),
        (1, ExclusivityScope.CATEGORY, 0.25),
        (30, ExclusivityScope.CATEGORY, 0.25),
        (31, ExclusivityScope.CATEGORY, 0.50),
        (89, ExclusivityScope.CATEGORY, 0.50),
        (90, ExclusivityScope.CATEGORY, 1.00),
        (365, ExclusivityScope.CATEGORY, 1.00),
        (30, ExclusivityScope.INDUSTRY, 0.25),
    ],
)
def test_exclusivity_uplift(config, days, scope, expected):
    assert exclusivity_uplift(Exclusivity(days=days, scope=scope), config) == expected


def test_engagement_adjustment_disabled_by_default(config):
    result = evaluate_pricing(_maya(), config)
    assert result.adjustments.engagement_factor == 1.0


def test_enabling_engagement_adjustment_fails_loudly(config):
    on = config.model_copy(deep=True)
    on.adjustments.engagement.enabled = True
    with pytest.raises(NotImplementedError):
        evaluate_pricing(_maya(), on)


# ---------- 完整评估 ----------


def test_maya_example():
    result = evaluate_pricing(_maya())

    assert result.line_items[0].tier == Tier.MICRO
    assert result.adjustments.niche_coefficient == 1.2
    assert result.adjusted_benchmark.p50 == 546.0
    assert result.offer_ratio == 0.5495
    assert result.score == 10
    assert result.position == OfferPosition.BELOW_P25
    assert (result.suggested_counter_range.low, result.suggested_counter_range.high) == (546.0, 834.0)


def test_uplifts_multiply(config, data):
    """付费使用 60 天（+20%）× 独家 45 天（+50%）= 1.8 倍。"""
    result = evaluate_pricing(
        _maya(
            usage_rights=UsageRights(type=UsageType.PAID_ADS, duration_days=60),
            exclusivity=Exclusivity(days=45, scope=ExclusivityScope.CATEGORY),
        ),
        config,
        data,
    )
    assert result.adjustments.total_multiplier == 1.8
    assert result.adjusted_benchmark.p50 == pytest.approx(546 * 1.8)


def test_multiple_deliverables_sum_up(config, data):
    """2 条 Reel + 3 条 Story（Micro 美妆）：(455×2 + 115×3) × 1.2。"""
    result = evaluate_pricing(
        _maya(deliverables=[{"type": "instagram_reel", "quantity": 2}, {"type": "instagram_story", "quantity": 3}]),
        config,
        data,
    )
    assert result.adjusted_benchmark.p50 == pytest.approx((455 * 2 + 115 * 3) * 1.2)


def test_multi_platform_uses_each_platform_tier(config, data):
    result = evaluate_pricing(
        _maya(
            followers={"instagram": 28_000, "tiktok": 120_000},
            deliverables=[{"type": "instagram_reel"}, {"type": "tiktok_video"}],
        ),
        config,
        data,
    )
    tiers = {li.type.value: li.tier for li in result.line_items}
    assert tiers == {"instagram_reel": Tier.MICRO, "tiktok_video": Tier.MID_PLUS}
    assert result.adjusted_benchmark.p50 == pytest.approx((455 + 1150) * 1.2)


@pytest.mark.parametrize(
    "offer, position",
    [
        (359.99, OfferPosition.BELOW_P25),
        (360, OfferPosition.P25_TO_P50),
        (545.99, OfferPosition.P25_TO_P50),
        (546, OfferPosition.P50_TO_P75),
        (834, OfferPosition.P50_TO_P75),
        (834.01, OfferPosition.ABOVE_P75),
    ],
)
def test_offer_position_boundaries(config, data, offer, position):
    assert evaluate_pricing(_maya(offer_usd=offer), config, data).position == position


@pytest.mark.parametrize(
    "offer, points",
    [
        (546, 45),  # 报价比 1.0
        (545, 35),
        (436.8, 35),  # 报价比正好 0.8
        (436, 22),
        (327.6, 22),  # 0.6
        (218.4, 10),  # 0.4
        (218, 0),
        (0, 0),  # 只送产品、不付现金
    ],
)
def test_offer_score_end_to_end(config, data, offer, points):
    assert evaluate_pricing(_maya(offer_usd=offer), config, data).score == points


def test_no_counter_offer_when_offer_reaches_p50(config, data):
    assert evaluate_pricing(_maya(offer_usd=546), config, data).suggested_counter_range is None
    assert evaluate_pricing(_maya(offer_usd=545), config, data).suggested_counter_range is not None


# ---------- 置信度 ----------


def test_low_confidence_has_notes_and_message():
    result = evaluate_pricing(_maya())
    assert result.confidence == Confidence.LOW
    assert result.confidence_notes
    assert "rough estimate" in result.confidence_message


def test_medium_confidence_has_no_message():
    """Nano 级 IG 图文帖 + 美妆（中置信度）+ 无加价 → 中置信度。"""
    result = evaluate_pricing(
        _maya(followers={"instagram": 5_000}, deliverables=[{"type": "instagram_feed_post"}], offer_usd=20)
    )
    assert result.confidence == Confidence.MEDIUM
    assert result.confidence_notes == []
    assert result.confidence_message is None


def test_low_confidence_niche_lowers_overall_confidence():
    result = evaluate_pricing(
        _maya(followers={"instagram": 5_000}, deliverables=[{"type": "instagram_feed_post"}], niche="gaming")
    )
    assert result.confidence == Confidence.LOW
    assert any("gaming" in n for n in result.confidence_notes)


def test_uplift_lowers_overall_confidence():
    result = evaluate_pricing(
        _maya(
            followers={"instagram": 5_000},
            deliverables=[{"type": "instagram_feed_post"}],
            exclusivity=Exclusivity(days=30, scope=ExclusivityScope.CATEGORY),
        )
    )
    assert result.confidence == Confidence.LOW
    assert any("Exclusivity" in n for n in result.confidence_notes)


# ---------- 配置与输入校验 ----------


def test_score_bands_come_from_config(config, data):
    stricter = config.model_copy(deep=True)
    stricter.score_bands[1].min_ratio = 0.9  # 35 分门槛从 0.8 提到 0.9
    assert evaluate_pricing(_maya(offer_usd=436.8), stricter, data).score == 22


def test_config_rejects_gap_between_tiers(config):
    raw = config.model_dump(mode="json")
    raw["tiers"][1]["min_followers"] = 12_000
    with pytest.raises(ValidationError):
        PricingConfig.model_validate(raw)


def test_config_rejects_unsorted_score_bands(config):
    raw = config.model_dump(mode="json")
    raw["score_bands"][0]["min_ratio"] = 0.5
    with pytest.raises(ValidationError):
        PricingConfig.model_validate(raw)


def test_missing_platform_followers_rejected():
    with pytest.raises(ValidationError):
        _maya(deliverables=[{"type": "tiktok_video"}])


@pytest.mark.parametrize(
    "overrides",
    [
        {"deliverables": []},
        {"deliverables": [{"type": "instagram_reel", "quantity": 0}]},
        {"offer_usd": -1},
        {"niche": "crypto"},
    ],
)
def test_invalid_pricing_input_rejected(overrides):
    with pytest.raises(ValidationError):
        _maya(**overrides)


# ---------- 接口 ----------


def test_pricing_endpoint():
    client = TestClient(app)
    response = client.post(
        "/api/rules/pricing",
        json={
            "followers": {"instagram": 28000},
            "niche": "beauty",
            "deliverables": [{"type": "instagram_reel"}],
            "offer_usd": 300,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["score"] == 10
    assert body["confidence"] == "low"


def test_pricing_endpoint_rejects_unsupported_followers():
    client = TestClient(app)
    response = client.post(
        "/api/rules/pricing",
        json={
            "followers": {"instagram": 800},
            "niche": "beauty",
            "deliverables": [{"type": "instagram_reel"}],
            "offer_usd": 300,
        },
    )
    assert response.status_code == 422
    assert "不在支持范围内" in response.json()["detail"]
