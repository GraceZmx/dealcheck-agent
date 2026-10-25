"""基准表数据完整性测试：缺格、空值、来源、置信度。"""

import itertools

import pytest
from pydantic import ValidationError

from app.config import BenchmarkData, load_benchmarks
from app.models import Confidence, DeliverableType, Niche, Platform, Tier


@pytest.fixture(scope="module")
def data() -> BenchmarkData:
    return load_benchmarks()


def test_every_deliverable_and_tier_has_a_price(data):
    for deliverable, tier in itertools.product(DeliverableType, Tier):
        row = data.base_price(deliverable, tier)
        assert row.p25 > 0


def test_no_duplicate_rows(data):
    keys = [(r.deliverable, r.tier) for r in data.base_prices]
    assert len(keys) == len(set(keys)) == len(DeliverableType) * len(Tier)


def test_all_120_platform_tier_niche_combinations_resolve(data):
    """3 平台 × 5 量级 × 8 领域 = 120 个组合，每个都能算出价格。"""
    combos = 0
    for platform, tier, niche in itertools.product(Platform, Tier, Niche):
        rows = [data.base_price(d, tier) for d in DeliverableType if d.platform == platform]
        coefficient = data.niche_coefficients[niche].coefficient
        assert rows and all(r.p50 * coefficient > 0 for r in rows)
        combos += 1
    assert combos == 120


def test_percentiles_are_ordered(data):
    for r in data.base_prices:
        assert r.p25 <= r.p50 <= r.p75, (r.deliverable, r.tier)


def test_prices_increase_with_tier(data):
    tiers = list(Tier)
    for deliverable in DeliverableType:
        p50s = [data.base_price(deliverable, t).p50 for t in tiers]
        assert p50s == sorted(p50s), deliverable


def test_every_row_cites_known_sources_and_method(data):
    for r in data.base_prices:
        assert r.sources and set(r.sources) <= set(data.sources)
        assert r.method.strip()


def test_every_source_has_url_and_date(data):
    for source in data.sources.values():
        assert source.url.startswith("https://")
        assert source.updated


def test_all_eight_niches_have_coefficients(data):
    assert set(data.niche_coefficients) == set(Niche)


@pytest.mark.parametrize(
    "niche, coefficient, confidence",
    [
        (Niche.PERSONAL_FINANCE, 2.0, Confidence.MEDIUM),
        (Niche.TECH, 1.5, Confidence.LOW),
        (Niche.BEAUTY, 1.2, Confidence.MEDIUM),
        (Niche.FITNESS_WELLNESS, 1.1, Confidence.MEDIUM),
        (Niche.FASHION, 1.0, Confidence.LOW),
        (Niche.FOOD, 1.0, Confidence.LOW),
        (Niche.TRAVEL_LIFESTYLE, 1.0, Confidence.LOW),
        (Niche.GAMING, 1.0, Confidence.LOW),
    ],
)
def test_niche_coefficients_match_reviewed_values(data, niche, coefficient, confidence):
    nc = data.niche_coefficients[niche]
    assert nc.coefficient == coefficient
    assert nc.confidence == confidence


def test_only_directly_sourced_cells_are_medium_confidence(data):
    medium = {(r.deliverable, r.tier) for r in data.base_prices if r.confidence == Confidence.MEDIUM}
    assert medium == {
        (DeliverableType.INSTAGRAM_FEED_POST, Tier.NANO),
        (DeliverableType.INSTAGRAM_REEL, Tier.NANO),
        (DeliverableType.INSTAGRAM_STORY, Tier.NANO),
        (DeliverableType.TIKTOK_VIDEO, Tier.NANO),
    }


@pytest.mark.parametrize(
    "deliverable, tier, expected",
    [
        (DeliverableType.INSTAGRAM_FEED_POST, Tier.MICRO, (150, 230, 350)),
        (DeliverableType.INSTAGRAM_REEL, Tier.MICRO, (300, 455, 695)),
        (DeliverableType.TIKTOK_VIDEO, Tier.MID_PLUS, (800, 1150, 1660)),
        (DeliverableType.YOUTUBE_DEDICATED, Tier.UPPER, (7420, 12180, 20000)),
    ],
)
def test_spot_check_reviewed_prices(data, deliverable, tier, expected):
    r = data.base_price(deliverable, tier)
    assert (r.p25, r.p50, r.p75) == expected


def test_data_rejects_unordered_percentiles(data):
    raw = data.model_dump(mode="json")
    raw["base_prices"][0].update(p25=100, p50=50, p75=200)
    with pytest.raises(ValidationError):
        BenchmarkData.model_validate(raw)


def test_data_rejects_unknown_source(data):
    raw = data.model_dump(mode="json")
    raw["base_prices"][0]["sources"] = ["made_up_report"]
    with pytest.raises(ValidationError):
        BenchmarkData.model_validate(raw)
