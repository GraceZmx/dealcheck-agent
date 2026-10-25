"""读取 backend/config/ 下的 YAML 配置和 backend/data/ 下的基准表，并用 Pydantic 校验格式和取值。

配置在每次调用时重新读取（文件很小），所以手动修改 YAML 后无需重启服务即可生效。
"""

import json
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from app.models import AdjustmentItem, Confidence, DeliverableType, Niche, Platform, Tier

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class ExclusivityRules(BaseModel):
    category_green_max_days: int = Field(ge=0)
    category_yellow_max_days: int = Field(ge=0)
    industry_yellow_max_days: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_order(self):
        if self.category_green_max_days > self.category_yellow_max_days:
            raise ValueError("category_green_max_days 不能大于 category_yellow_max_days")
        return self


class UsageRightsRules(BaseModel):
    paid_green_max_days: int = Field(ge=0)
    paid_yellow_max_days: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_order(self):
        if self.paid_green_max_days > self.paid_yellow_max_days:
            raise ValueError("paid_green_max_days 不能大于 paid_yellow_max_days")
        return self


class KillFeeRules(BaseModel):
    green_min_percent: float = Field(ge=0, le=100)
    yellow_min_percent: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _check_order(self):
        if self.yellow_min_percent > self.green_min_percent:
            raise ValueError("yellow_min_percent 不能大于 green_min_percent")
        return self


class RevisionRules(BaseModel):
    green_max: int = Field(ge=0)
    yellow_max: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_order(self):
        if self.green_max > self.yellow_max:
            raise ValueError("green_max 不能大于 yellow_max")
        return self


class ContractRulesConfig(BaseModel):
    exclusivity: ExclusivityRules
    usage_rights: UsageRightsRules
    kill_fee: KillFeeRules
    revisions: RevisionRules


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_contract_rules(path: Path | None = None) -> ContractRulesConfig:
    """读取合同条款阈值。path 为空时读取默认的 contract_rules.yaml。"""
    return ContractRulesConfig.model_validate(load_yaml(path or CONFIG_DIR / "contract_rules.yaml"))


# ---------- 报价评估配置（pricing.yaml）----------


class TierDef(BaseModel):
    name: Tier
    min_followers: int = Field(ge=0)
    max_followers: int = Field(gt=0)


class ScoreBand(BaseModel):
    min_ratio: float = Field(ge=0)
    points: int = Field(ge=0)


class EngagementAdjustment(BaseModel):
    enabled: bool = False


class UsageRightsUplift(BaseModel):
    paid_not_billed_uplift: float = Field(ge=0)
    long_or_perpetual_not_billed_uplift: float = Field(ge=0)
    long_threshold_days: int = Field(ge=0)


class ExclusivityUplift(BaseModel):
    max_days: int | None = Field(None, ge=0)
    uplift: float = Field(ge=0)


class PricingAdjustments(BaseModel):
    engagement: EngagementAdjustment
    usage_rights: UsageRightsUplift
    exclusivity: list[ExclusivityUplift] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_exclusivity_bands(self):
        days = [b.max_days for b in self.exclusivity]
        if days[-1] is not None or None in days[:-1]:
            raise ValueError("exclusivity 档位只有最后一档的 max_days 可以为空")
        if days[:-1] != sorted(days[:-1]):
            raise ValueError("exclusivity 档位需按 max_days 从小到大排列")
        return self


class PricingConfig(BaseModel):
    tiers: list[TierDef] = Field(min_length=1)
    score_bands: list[ScoreBand] = Field(min_length=1)
    adjustments: PricingAdjustments

    @model_validator(mode="after")
    def _check(self):
        for prev, cur in zip(self.tiers, self.tiers[1:]):
            if prev.max_followers != cur.min_followers:
                raise ValueError("tiers 必须首尾相接、从小到大排列")
        ratios = [b.min_ratio for b in self.score_bands]
        if ratios != sorted(ratios, reverse=True) or ratios[-1] != 0:
            raise ValueError("score_bands 需按 min_ratio 从大到小排列，且最后一档为 0")
        return self

    @property
    def max_score(self) -> int:
        return max(b.points for b in self.score_bands)


def load_pricing_config(path: Path | None = None) -> PricingConfig:
    """读取报价评估配置。path 为空时读取默认的 pricing.yaml。"""
    return PricingConfig.model_validate(load_yaml(path or CONFIG_DIR / "pricing.yaml"))


# ---------- 市场价基准表（benchmarks_v0.json）----------


class SourceInfo(BaseModel):
    title: str
    url: str
    updated: str


class BasePrice(BaseModel):
    platform: Platform
    deliverable: DeliverableType
    tier: Tier
    p25: float = Field(gt=0)
    p50: float = Field(gt=0)
    p75: float = Field(gt=0)
    confidence: Confidence
    sources: list[str] = Field(min_length=1)
    method: str


class NicheCoefficient(BaseModel):
    coefficient: float = Field(gt=0)
    confidence: Confidence
    sources: list[str]
    note: str


class BenchmarkData(BaseModel):
    version: str
    currency: str
    data_date: str
    sources: dict[str, SourceInfo]
    base_prices: list[BasePrice]
    niche_coefficients: dict[Niche, NicheCoefficient]

    @model_validator(mode="after")
    def _check(self):
        known = set(self.sources)
        for row in self.base_prices:
            if not row.p25 <= row.p50 <= row.p75:
                raise ValueError(f"{row.deliverable.value}/{row.tier.value}: 需满足 P25 ≤ P50 ≤ P75")
            if row.platform != row.deliverable.platform:
                raise ValueError(f"{row.deliverable.value}: 平台不匹配")
            if unknown := set(row.sources) - known:
                raise ValueError(f"{row.deliverable.value}/{row.tier.value}: 未知来源 {unknown}")
        for niche, nc in self.niche_coefficients.items():
            if unknown := set(nc.sources) - known:
                raise ValueError(f"{niche.value}: 未知来源 {unknown}")
        return self

    def base_price(self, deliverable: DeliverableType, tier: Tier) -> BasePrice:
        for row in self.base_prices:
            if row.deliverable == deliverable and row.tier == tier:
                return row
        raise KeyError(f"基准表缺少 {deliverable.value} / {tier.value}")


def load_benchmarks(path: Path | None = None) -> BenchmarkData:
    """读取市场价基准表。path 为空时读取默认的 benchmarks_v0.json。"""
    with open(path or DATA_DIR / "benchmarks_v0.json", encoding="utf-8") as f:
        return BenchmarkData.model_validate(json.load(f))


# ---------- 硬性规则配置（hard_rules.yaml）----------


class HardRulesConfig(BaseModel):
    severe_negative_min_independent_sources: int = Field(ge=1)
    detected_category_min_sources: int = Field(ge=1)
    custom_category_min_sources: int = Field(ge=1)


def load_hard_rules_config(path: Path | None = None) -> HardRulesConfig:
    return HardRulesConfig.model_validate(load_yaml(path or CONFIG_DIR / "hard_rules.yaml"))


# ---------- 打分与决策配置（scoring.yaml）----------


class ContractScoring(BaseModel):
    max_score: int = Field(gt=0)
    red_deduction: int = Field(ge=0)
    yellow_deduction: int = Field(ge=0)


class BrandFitScoring(BaseModel):
    max_score: int = Field(gt=0)
    default_score: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self):
        if self.default_score > self.max_score:
            raise ValueError("default_score 不能大于 max_score")
        return self


class AdjustmentRange(BaseModel):
    min: int = Field(le=0)
    max: int = Field(ge=0)


class AdjustmentScoring(BaseModel):
    brand_reputation: AdjustmentRange
    values_fit: AdjustmentRange
    peer_rate: AdjustmentRange
    total_cap: int = Field(ge=0)

    def range_for(self, item: AdjustmentItem) -> AdjustmentRange:
        return getattr(self, item.value)


class DecisionThresholds(BaseModel):
    accept_min_score: int = Field(ge=0)
    negotiate_min_score: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self):
        if self.negotiate_min_score > self.accept_min_score:
            raise ValueError("negotiate_min_score 不能大于 accept_min_score")
        return self


class ScoringConfig(BaseModel):
    contract: ContractScoring
    brand_fit: BrandFitScoring
    adjustments: AdjustmentScoring
    decision: DecisionThresholds


def load_scoring_config(path: Path | None = None) -> ScoringConfig:
    return ScoringConfig.model_validate(load_yaml(path or CONFIG_DIR / "scoring.yaml"))


# ---------- Agent 配置（agent.yaml）----------


class ModelSettings(BaseModel):
    name: str
    thinking: bool = False
    reasoning_effort: str = "low"
    max_tokens: int = Field(gt=0)
    temperature: float = Field(ge=0, le=2)
    request_timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)


class AgentBudget(BaseModel):
    max_searches: int = Field(ge=0)
    max_fetches: int = Field(ge=0)
    max_model_calls: int = Field(ge=2)  # 至少一次调查 + 一次收尾
    max_seconds: float = Field(gt=0)
    max_cost_cny: float = Field(gt=0)
    max_format_retries: int = Field(ge=0)


class SearchSettings(BaseModel):
    results_per_query: int = Field(ge=1, le=50)
    summary_max_chars: int = Field(gt=0)
    fetch_max_chars: int = Field(gt=0)
    query_max_chars: int = Field(ge=1, le=128)


class ModelPrice(BaseModel):
    input_cache_hit: float = Field(ge=0)
    input_cache_miss: float = Field(ge=0)
    output: float = Field(ge=0)


class PricingTable(BaseModel):
    usd_to_cny: float = Field(gt=0)
    models: dict[str, ModelPrice]
    search_per_request_usd: float = Field(ge=0)
    fetch_per_request_usd: float = Field(ge=0)


class EmailSettings(BaseModel):
    max_tokens: int = Field(gt=0)
    temperature: float = Field(ge=0, le=2)


class AgentConfig(BaseModel):
    model: ModelSettings
    budget: AgentBudget
    search: SearchSettings
    pricing: PricingTable
    email: EmailSettings


def load_agent_config(path: Path | None = None) -> AgentConfig:
    return AgentConfig.model_validate(load_yaml(path or CONFIG_DIR / "agent.yaml"))
