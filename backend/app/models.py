"""数据格式定义：输入（合作信息）和输出（评估结果）各长什么样。"""

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


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
