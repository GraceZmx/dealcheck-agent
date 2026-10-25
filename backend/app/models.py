"""数据格式定义：输入（合作信息）和输出（评估结果）各长什么样。"""

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


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
