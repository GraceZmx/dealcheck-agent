"""合同条款红黄绿灯测试：覆盖 PRD 5.5 节每个阈值的两侧。"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import ContractRulesConfig, load_contract_rules
from app.main import app
from app.models import (
    ContractTerms,
    Exclusivity,
    ExclusivityScope,
    Light,
    UsageRights,
    UsageType,
)
from app.rules.contract import (
    check_contract,
    check_exclusivity,
    check_kill_fee,
    check_revisions,
    check_usage_rights,
)

G, Y, R = Light.GREEN, Light.YELLOW, Light.RED


@pytest.fixture
def rules() -> ContractRulesConfig:
    return load_contract_rules()


# ---------- 独家期 ----------


@pytest.mark.parametrize(
    "days, expected",
    [(1, G), (30, G), (31, Y), (60, Y), (90, Y), (91, R), (365, R)],
)
def test_exclusivity_category(rules, days, expected):
    ex = Exclusivity(days=days, scope=ExclusivityScope.CATEGORY)
    assert check_exclusivity(ex, rules).light == expected


@pytest.mark.parametrize("days, expected", [(1, Y), (30, Y), (31, R), (90, R)])
def test_exclusivity_industry(rules, days, expected):
    ex = Exclusivity(days=days, scope=ExclusivityScope.INDUSTRY)
    assert check_exclusivity(ex, rules).light == expected


@pytest.mark.parametrize(
    "ex",
    [
        Exclusivity(days=0, scope=ExclusivityScope.NONE),
        Exclusivity(days=999, scope=ExclusivityScope.NONE),
        Exclusivity(days=0, scope=ExclusivityScope.INDUSTRY),
    ],
)
def test_no_exclusivity_is_green(rules, ex):
    assert check_exclusivity(ex, rules).light == G


# ---------- 使用权 ----------


def _paid(days, billed_separately=False, usage_type=UsageType.PAID_ADS):
    return UsageRights(type=usage_type, duration_days=days, paid_usage_billed_separately=billed_separately)


def test_usage_organic_is_green(rules):
    assert check_usage_rights(UsageRights(type=UsageType.ORGANIC), rules).light == G


@pytest.mark.parametrize(
    "days, billed_separately, expected",
    [
        (0, False, G),
        (30, False, G),
        (30, True, G),
        (31, False, Y),
        (31, True, Y),
        (180, False, Y),
        (180, True, Y),
        (181, True, Y),  # 超过 6 个月但单独计费 → 黄
        (181, False, R),  # 超过 6 个月且没有额外费用 → 红
        (365, False, R),
    ],
)
def test_usage_paid_duration(rules, days, billed_separately, expected):
    assert check_usage_rights(_paid(days, billed_separately), rules).light == expected


@pytest.mark.parametrize("days, expected", [(30, G), (31, Y), (181, R)])
def test_usage_whitelisting_follows_paid_rules(rules, days, expected):
    ur = _paid(days, usage_type=UsageType.WHITELISTING)
    assert check_usage_rights(ur, rules).light == expected


@pytest.mark.parametrize("usage_type", list(UsageType))
@pytest.mark.parametrize("billed_separately", [False, True])
def test_usage_perpetual_is_always_red(rules, usage_type, billed_separately):
    ur = UsageRights(type=usage_type, perpetual=True, paid_usage_billed_separately=billed_separately)
    assert check_usage_rights(ur, rules).light == R


def test_usage_paid_without_duration_is_red(rules):
    result = check_usage_rights(UsageRights(type=UsageType.PAID_ADS, duration_days=None), rules)
    assert result.light == R
    assert "no stated end date" in result.risk


# ---------- 撤单费 ----------


@pytest.mark.parametrize(
    "percent, expected",
    [(100, G), (50, G), (49.9, Y), (49, Y), (25, Y), (24.9, R), (24, R), (0, R), (None, R)],
)
def test_kill_fee(rules, percent, expected):
    assert check_kill_fee(percent, rules).light == expected


# ---------- 修改次数 ----------


@pytest.mark.parametrize(
    "revisions, expected",
    [(0, G), (1, G), (2, G), (3, Y), (4, R), (10, R), ("unlimited", R)],
)
def test_revisions(rules, revisions, expected):
    assert check_revisions(revisions, rules).light == expected


# ---------- 提示文案 ----------


def test_green_has_no_risk_or_suggestion(rules):
    result = check_kill_fee(50, rules)
    assert result.risk is None and result.suggestion is None


@pytest.mark.parametrize("percent", [30, 10, None])
def test_non_green_has_risk_and_suggestion(rules, percent):
    result = check_kill_fee(percent, rules)
    assert result.risk and result.suggestion


# ---------- 整份合同 ----------


def test_example_contract():
    """独家期 60 天 + 永久使用权 + 撤单费未提及 + 修改 2 次。"""
    terms = ContractTerms(
        exclusivity=Exclusivity(days=60, scope=ExclusivityScope.CATEGORY),
        usage_rights=UsageRights(type=UsageType.PAID_ADS, perpetual=True),
        kill_fee_percent=None,
        revisions=2,
    )
    result = check_contract(terms)

    lights = {t.term.value: t.light for t in result.terms}
    assert lights == {"exclusivity": Y, "usage_rights": R, "kill_fee": R, "revisions": G}
    assert result.red_count == 2
    assert result.yellow_count == 1


def test_all_green_contract():
    terms = ContractTerms(kill_fee_percent=50, revisions=2)
    result = check_contract(terms)
    assert result.red_count == 0 and result.yellow_count == 0


# ---------- 输入校验 ----------


@pytest.mark.parametrize(
    "bad_input",
    [
        {"revisions": -1},
        {"revisions": "many"},
        {"revisions": 2, "kill_fee_percent": 101},
        {"revisions": 2, "kill_fee_percent": -5},
        {"revisions": 2, "exclusivity": {"days": -1, "scope": "category"}},
        {"revisions": 2, "exclusivity": {"days": 10, "scope": "global"}},
    ],
)
def test_invalid_input_is_rejected(bad_input):
    with pytest.raises(ValidationError):
        ContractTerms.model_validate(bad_input)


# ---------- 阈值确实来自配置 ----------


def test_thresholds_come_from_config(rules):
    """把绿灯上限从 30 改成 45 天后，31 天应从黄灯变为绿灯。"""
    ex = Exclusivity(days=31, scope=ExclusivityScope.CATEGORY)
    assert check_exclusivity(ex, rules).light == Y

    looser = rules.model_copy(deep=True)
    looser.exclusivity.category_green_max_days = 45
    assert check_exclusivity(ex, looser).light == G


def test_rules_loaded_from_yaml_file(tmp_path):
    """从一个改过数字的 YAML 文件读取，判定结果随之改变。"""
    custom = tmp_path / "contract_rules.yaml"
    custom.write_text(
        """
exclusivity: {category_green_max_days: 45, category_yellow_max_days: 90, industry_yellow_max_days: 30}
usage_rights: {paid_green_max_days: 30, paid_yellow_max_days: 180}
kill_fee: {green_min_percent: 50, yellow_min_percent: 25}
revisions: {green_max: 2, yellow_max: 3}
""",
        encoding="utf-8",
    )
    rules = load_contract_rules(custom)
    ex = Exclusivity(days=31, scope=ExclusivityScope.CATEGORY)
    assert check_exclusivity(ex, rules).light == G


def test_suggestion_text_follows_config(rules):
    stricter = rules.model_copy(deep=True)
    stricter.kill_fee.green_min_percent = 60
    assert "60%" in check_kill_fee(None, stricter).suggestion


def test_config_rejects_inverted_thresholds():
    with pytest.raises(ValidationError):
        ContractRulesConfig.model_validate(
            {
                "exclusivity": {"category_green_max_days": 100, "category_yellow_max_days": 90, "industry_yellow_max_days": 30},
                "usage_rights": {"paid_green_max_days": 30, "paid_yellow_max_days": 180},
                "kill_fee": {"green_min_percent": 50, "yellow_min_percent": 25},
                "revisions": {"green_max": 2, "yellow_max": 3},
            }
        )


# ---------- 接口 ----------


def test_contract_check_endpoint():
    client = TestClient(app)
    response = client.post(
        "/api/rules/contract-check",
        json={
            "exclusivity": {"days": 60, "scope": "category"},
            "usage_rights": {"type": "paid_ads", "perpetual": True},
            "kill_fee_percent": None,
            "revisions": 2,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["red_count"] == 2
    assert body["yellow_count"] == 1


def test_contract_check_endpoint_rejects_bad_input():
    client = TestClient(app)
    response = client.post("/api/rules/contract-check", json={"revisions": "many"})
    assert response.status_code == 422
