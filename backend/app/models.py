"""数据格式定义：输入（合作信息）和输出（评估结果）各长什么样。"""

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------- 合同条款输入（对应 PRD 5.3 节表单）----------


class ExclusivityScope(str, Enum):
    NONE = "none"  # 无独家要求
    CATEGORY = "category"  # 仅限同品类竞品
    INDUSTRY = "industry"  # 全行业


class Exclusivity(BaseModel):
    days: int = Field(0, ge=0, description="独家期天数")
    scope: ExclusivityScope = ExclusivityScope.NONE


class UsageType(str, Enum):
    ORGANIC = "organic"  # 仅自然发布
    PAID_ADS = "paid_ads"  # 付费投放
    WHITELISTING = "whitelisting"  # 白名单投放


class UsageRights(BaseModel):
    type: UsageType = UsageType.ORGANIC
    duration_days: int | None = Field(
        None, ge=0, description="付费投放 / 白名单的使用天数；None 表示合同未写明期限"
    )
    perpetual: bool = Field(False, description="是否为永久使用权")
    paid_usage_billed_separately: bool = Field(
        False, description="付费使用是否在报价之外单独计费"
    )


class ContractTerms(BaseModel):
    exclusivity: Exclusivity = Exclusivity()
    usage_rights: UsageRights = UsageRights()
    kill_fee_percent: float | None = Field(
        None, ge=0, le=100, description="撤单费百分比；None 表示合同未提及"
    )
    revisions: Annotated[int, Field(ge=0)] | Literal["unlimited"] = Field(
        ..., description='修改次数；"unlimited" 表示无限次'
    )


# ---------- 合同条款检查结果 ----------


class Light(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class TermName(str, Enum):
    EXCLUSIVITY = "exclusivity"
    USAGE_RIGHTS = "usage_rights"
    KILL_FEE = "kill_fee"
    REVISIONS = "revisions"


class TermResult(BaseModel):
    term: TermName
    light: Light
    risk: str | None = Field(None, description="为什么有风险（绿灯时为空）")
    suggestion: str | None = Field(None, description="建议怎么改（绿灯时为空）")


class ContractCheckResult(BaseModel):
    terms: list[TermResult]
    red_count: int
    yellow_count: int


# ---------- 报价评估输入（对应 PRD 5.3 / 5.4 节）----------


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"


class DeliverableType(str, Enum):
    INSTAGRAM_FEED_POST = "instagram_feed_post"
    INSTAGRAM_REEL = "instagram_reel"
    INSTAGRAM_STORY = "instagram_story"
    TIKTOK_VIDEO = "tiktok_video"
    YOUTUBE_INTEGRATION = "youtube_integration"
    YOUTUBE_DEDICATED = "youtube_dedicated"

    @property
    def platform(self) -> Platform:
        return Platform(self.value.split("_", 1)[0])


class Niche(str, Enum):
    BEAUTY = "beauty"
    FASHION = "fashion"
    FITNESS_WELLNESS = "fitness_wellness"
    FOOD = "food"
    TECH = "tech"
    GAMING = "gaming"
    TRAVEL_LIFESTYLE = "travel_lifestyle"
    PERSONAL_FINANCE = "personal_finance"


class Tier(str, Enum):
    NANO = "nano"
    MICRO = "micro"
    MID = "mid"
    MID_PLUS = "mid_plus"
    UPPER = "upper"


class Confidence(str, Enum):
    MEDIUM = "medium"
    LOW = "low"


class Deliverable(BaseModel):
    type: DeliverableType
    quantity: int = Field(1, ge=1)


class PricingInput(BaseModel):
    followers: dict[Platform, int] = Field(description="各平台粉丝数；交付内容涉及的平台都必须填写")
    niche: Niche
    deliverables: list[Deliverable] = Field(min_length=1)
    offer_usd: float = Field(ge=0, description="现金报价（USD），不含赠品")
    usage_rights: UsageRights = UsageRights()
    exclusivity: Exclusivity = Exclusivity()

    @model_validator(mode="after")
    def _followers_cover_deliverables(self):
        missing = {d.type.platform for d in self.deliverables} - set(self.followers)
        if missing:
            names = ", ".join(sorted(p.value for p in missing))
            raise ValueError(f"缺少以下平台的粉丝数：{names}")
        return self


# ---------- 报价评估结果 ----------


class PriceRange(BaseModel):
    p25: float
    p50: float
    p75: float


class LineItem(BaseModel):
    type: DeliverableType
    quantity: int
    tier: Tier
    base_unit_price: PriceRange = Field(description="基准表单价（未乘领域系数）")
    unit_price: PriceRange = Field(description="乘领域系数后的单价")
    confidence: Confidence


class Adjustments(BaseModel):
    niche_coefficient: float
    usage_rights_uplift: float
    exclusivity_uplift: float
    engagement_factor: float
    total_multiplier: float = Field(description="(1 + 使用权加价) × (1 + 独家期加价) × 互动率系数")


class CounterRange(BaseModel):
    low: float
    high: float


class OfferPosition(str, Enum):
    BELOW_P25 = "below_p25"
    P25_TO_P50 = "p25_to_p50"
    P50_TO_P75 = "p50_to_p75"
    ABOVE_P75 = "above_p75"


class PricingResult(BaseModel):
    line_items: list[LineItem]
    adjustments: Adjustments
    adjusted_benchmark: PriceRange = Field(description="调整后基准（全部交付内容合计）")
    offer_usd: float
    offer_ratio: float = Field(description="报价 ÷ 调整后基准 P50")
    position: OfferPosition
    score: int
    max_score: int
    suggested_counter_range: CounterRange | None = Field(
        None, description="建议还价区间（调整后基准 P50–P75）；报价已达 P50 时为空"
    )
    confidence: Confidence
    confidence_notes: list[str] = Field(description="置信度偏低的具体原因")
    confidence_message: str | None = Field(None, description="低置信度时给用户的一句提示")
    data_version: str
    data_date: str


# ---------- 创作者档案与合作信息（对应 PRD 5.2 / 5.3 节）----------


class SensitiveCategory(str, Enum):
    """标准敏感品类：创作者拒接清单、表单勾选、Agent 报告共用这套名称，便于规则精确比对。

    清单本身不代表"不能接"，只有命中创作者自己的拒接清单才会触发 HR-1。
    """

    ALCOHOL = "alcohol"
    TOBACCO_VAPING = "tobacco_vaping"
    CANNABIS = "cannabis"
    GAMBLING = "gambling"
    CRYPTO = "crypto"
    WEIGHT_LOSS_SUPPLEMENTS = "weight_loss_supplements"
    ADULT_CONTENT = "adult_content"
    FIREARMS = "firearms"
    FAST_FASHION = "fast_fashion"  # 快时尚
    MLM = "mlm"  # 传销式多层次营销
    SUPPLEMENTS_PHARMA = "supplements_pharma"  # 保健品与处方药
    POLITICAL_CONTENT = "political_content"  # 政治类内容
    PAYDAY_LOANS = "payday_loans"  # 高利贷与现金贷


class CreatorProfile(BaseModel):
    followers: dict[Platform, int] = Field(description="各平台粉丝数")
    niche: Niche
    blocked_categories: list[SensitiveCategory] = Field(default_factory=list, description="拒接品类（标准清单）")
    blocked_custom: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list,
        max_length=10,
        description="其他拒接品类（自由填写）；由 Agent 做语义判断，命中只触发封顶类规则 HR-8",
    )

    @field_validator("blocked_custom")
    @classmethod
    def _strip_custom(cls, items: list[str]) -> list[str]:
        cleaned = [i.strip() for i in items]
        if any(not i for i in cleaned):
            raise ValueError("自由填写的拒接品类不能为空白")
        return cleaned
    rate_floor_usd: float | None = Field(None, ge=0, description="最低接单价；为空表示未设置")
    values: list[Annotated[str, Field(min_length=1, max_length=50)]] = Field(
        default_factory=list, max_length=10, description="价值观与偏好标签，例如 cruelty-free、sustainability"
    )


class DealInput(BaseModel):
    brand_name: str = Field(min_length=1)
    brand_website: str | None = None
    brand_categories: list[SensitiveCategory] = Field(
        default_factory=list, description="品牌涉及的敏感品类（用户在表单中勾选）"
    )
    deliverables: list[Deliverable] = Field(min_length=1)
    offer_usd: float = Field(ge=0)
    contract: ContractTerms
    requires_upfront_payment: bool = Field(False, description="是否要求创作者预付任何费用")


# ---------- Agent 调查结果（对应 PRD 附录 B；M1 用手工编写的模拟数据）----------


class SourceRef(BaseModel):
    id: str
    url: str
    title: str | None = None


class SevereFlagType(str, Enum):
    SCAM = "scam"
    REGULATORY = "regulatory"
    NONPAYMENT = "nonpayment"
    SAFETY = "safety"


class SevereFlag(BaseModel):
    type: SevereFlagType
    description: str
    source_ids: list[str] = Field(default_factory=list)


class DetectedCategory(BaseModel):
    category: SensitiveCategory
    source_ids: list[str] = Field(default_factory=list)


class CustomCategoryMatch(BaseModel):
    """Agent 判断品牌和创作者某个自由填写的拒接品类语义相符。"""

    blocked_text: str = Field(description="创作者填写的原文，必须和档案中的某一项一致")
    explanation: str
    source_ids: list[str] = Field(default_factory=list)


class RiskLevel(str, Enum):
    NONE = "none"
    MINOR = "minor"
    MODERATE = "moderate"
    SEVERE = "severe"
    INSUFFICIENT_INFO = "insufficient_info"


class FitLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT_INFO = "insufficient_info"


class PeerRate(BaseModel):
    description: str
    amount_usd: float | None = Field(None, ge=0)
    source_ids: list[str] = Field(default_factory=list)


class ValuesFit(BaseModel):
    level: FitLevel = FitLevel.INSUFFICIENT_INFO
    summary: str = ""
    source_ids: list[str] = Field(default_factory=list)


class BrandOverview(BaseModel):
    """品牌调查摘要，按固定小节组织（每节 1–3 句，保留来源编号）。"""

    who_they_are: str = ""
    working_with_creators: str = ""
    regulatory_and_controversies: str = ""

    def sections(self) -> list[tuple[str, str]]:
        return [("Who they are", self.who_they_are), ("Working with creators", self.working_with_creators),
                ("Regulatory & controversies", self.regulatory_and_controversies)]


class AdjustmentItem(str, Enum):
    BRAND_REPUTATION = "brand_reputation"
    VALUES_FIT = "values_fit"
    PEER_RATE = "peer_rate"


class ScoreAdjustment(BaseModel):
    item: AdjustmentItem
    delta: int
    reason: str
    source_ids: list[str] = Field(default_factory=list)


class AgentSubmission(BaseModel):
    """Agent 通过 submit_findings 工具提交的结论。来源列表不由 Agent 填写，由程序根据登记表生成。"""

    brand_entity_confirmed: bool
    brand_overview: BrandOverview = BrandOverview()
    risk_level: RiskLevel = RiskLevel.INSUFFICIENT_INFO
    severe_flags: list[SevereFlag] = Field(default_factory=list)
    detected_categories: list[DetectedCategory] = Field(default_factory=list)
    custom_category_matches: list[CustomCategoryMatch] = Field(default_factory=list)
    peer_rates: list[PeerRate] = Field(default_factory=list)
    values_fit: ValuesFit = ValuesFit()
    score_adjustments: list[ScoreAdjustment] = Field(default_factory=list)


class AgentFindings(BaseModel):
    brand_entity_confirmed: bool
    brand_overview: BrandOverview = BrandOverview()
    risk_level: RiskLevel = RiskLevel.INSUFFICIENT_INFO
    peer_rates: list[PeerRate] = Field(default_factory=list)
    values_fit: ValuesFit = ValuesFit()
    severe_flags: list[SevereFlag] = Field(default_factory=list)
    detected_categories: list[DetectedCategory] = Field(default_factory=list)
    custom_category_matches: list[CustomCategoryMatch] = Field(default_factory=list)
    score_adjustments: list[ScoreAdjustment] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    unverified_citations: list[str] = Field(
        default_factory=list, description="Agent 引用了但本次搜索中不存在的编号（护栏 G12），已从正文中删除"
    )

    @property
    def source_map(self) -> dict[str, SourceRef]:
        return {s.id: s for s in self.sources}


class EvaluationRequest(BaseModel):
    profile: CreatorProfile
    deal: DealInput
    agent_findings: AgentFindings | None = Field(None, description="为空表示仅规则模式")


# ---------- 硬性规则、打分与决策结果 ----------


class HardRuleKind(str, Enum):
    REJECT = "reject"  # 拒绝类：直接建议拒绝
    CAP = "cap"  # 封顶类：最多到"谈判"


class HardRuleHit(BaseModel):
    rule_id: str
    kind: HardRuleKind
    message: str
    evidence_urls: list[str] = Field(default_factory=list)


class AdjustmentStatus(str, Enum):
    APPLIED = "applied"
    CLAMPED = "clamped"  # 超出单项范围，被截断
    VOIDED = "voided"  # 没有有效来源，按 0 计


class AppliedAdjustment(BaseModel):
    item: AdjustmentItem
    requested: int
    applied: int
    status: AdjustmentStatus
    reason: str
    source_urls: list[str] = Field(default_factory=list)


class DimensionScore(BaseModel):
    base: int
    adjustment: int
    final: int
    max: int


class ScoreBreakdown(BaseModel):
    pricing: DimensionScore
    contract: DimensionScore
    brand_fit: DimensionScore
    adjustments: list[AppliedAdjustment]
    total_cap_correction: int = Field(0, description="调分合计超过 ±total_cap 时的修正值；未超出时为 0")
    total: int


class Decision(str, Enum):
    ACCEPT = "accept"
    NEGOTIATE = "negotiate"
    DECLINE = "decline"


class EvaluationResult(BaseModel):
    decision: Decision
    total_score: int
    reasons: list[str]
    hard_rules: list[HardRuleHit]
    score: ScoreBreakdown
    pricing: PricingResult
    contract: ContractCheckResult
    brand_research_available: bool
