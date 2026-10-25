"""读取 backend/config/ 下的 YAML 配置，并用 Pydantic 校验格式和取值。

配置在每次调用时重新读取（文件很小），所以手动修改 YAML 后无需重启服务即可生效。
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


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
