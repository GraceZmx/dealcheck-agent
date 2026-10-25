"""硬性规则测试：HR-1 ~ HR-7 的命中与不命中。HR-2 / HR-6 / HR-7 用模拟的 Agent 结果。"""

import pytest
from pydantic import ValidationError

from app.config import load_contract_rules, load_hard_rules_config
from app.models import (
    AgentFindings,
    ContractTerms,
    CreatorProfile,
    DealInput,
    HardRuleKind,
)
from app.rules.contract import check_contract
from app.rules.evidence import domain_of, independent_source_count, resolve_urls
from app.rules.hard_rules import evaluate_hard_rules

REJECT, CAP = HardRuleKind.REJECT, HardRuleKind.CAP


@pytest.fixture(scope="module")
def hr_config():
    return load_hard_rules_config()


def profile(**overrides) -> CreatorProfile:
    fields = dict(followers={"instagram": 28_000}, niche="beauty", blocked_categories=["alcohol"], rate_floor_usd=250)
    fields.update(overrides)
    return CreatorProfile(**fields)


def deal(**overrides) -> DealInput:
    fields = dict(
        brand_name="Test Brand",
        deliverables=[{"type": "instagram_reel"}],
        offer_usd=500,
        contract={"kill_fee_percent": 50, "revisions": 2},
    )
    fields.update(overrides)
    return DealInput(**fields)


def findings(**overrides) -> AgentFindings:
    fields = dict(
        brand_entity_confirmed=True,
        sources=[
            {"id": "a1", "url": "https://news-a.example/story"},
            {"id": "a2", "url": "https://news-a.example/follow-up"},
            {"id": "b1", "url": "https://news-b.example/report"},
            {"id": "w1", "url": "https://www.news-a.example/third"},
            {"id": "bad", "url": "not-a-url"},
        ],
    )
    fields.update(overrides)
    return AgentFindings(**fields)


def run(p=None, d=None, f=None, config=None):
    p, d = p or profile(), d or deal()
    contract = check_contract(d.contract, load_contract_rules())
    return evaluate_hard_rules(p, d, contract, f, config or load_hard_rules_config())


def ids(hits):
    return [h.rule_id for h in hits]


def test_clean_deal_has_no_hits():
    assert run(f=findings()) == []


# ---------- HR-1 拒接品类 ----------


def test_hr1_form_category_in_blocklist():
    hits = run(d=deal(brand_categories=["alcohol"]))
    assert ids(hits) == ["HR-1"] and hits[0].kind == REJECT


def test_hr1_form_category_not_in_blocklist():
    assert run(d=deal(brand_categories=["crypto"])) == []


def test_hr1_agent_detected_category_with_source():
    hits = run(f=findings(detected_categories=[{"category": "alcohol", "source_ids": ["b1"]}]))
    assert ids(hits) == ["HR-1"]
    assert hits[0].evidence_urls == ["https://news-b.example/report"]


@pytest.mark.parametrize("source_ids", [[], ["missing"], ["bad"]])
def test_hr1_agent_detected_category_without_valid_source_is_ignored(source_ids):
    f = findings(detected_categories=[{"category": "alcohol", "source_ids": source_ids}])
    assert run(f=f) == []


def test_hr1_agent_detected_category_not_blocked():
    f = findings(detected_categories=[{"category": "crypto", "source_ids": ["b1"]}])
    assert run(f=f) == []


def test_hr1_form_and_agent_both_report():
    f = findings(detected_categories=[{"category": "alcohol", "source_ids": ["b1"]}])
    hits = run(d=deal(brand_categories=["alcohol"]), f=f)
    assert ids(hits) == ["HR-1", "HR-1"]  # 一条来自表单，一条来自 Agent（附证据）


# ---------- HR-2 / HR-6 严重负面 ----------


def _flag(source_ids):
    return [{"type": "nonpayment", "description": "Late payments.", "source_ids": source_ids}]


def test_hr2_two_independent_sources_rejects():
    hits = run(f=findings(severe_flags=_flag(["a1", "b1"])))
    assert ids(hits) == ["HR-2"] and hits[0].kind == REJECT
    assert len(hits[0].evidence_urls) == 2


def test_same_site_counts_as_one_source():
    hits = run(f=findings(severe_flags=_flag(["a1", "a2"])))
    assert ids(hits) == ["HR-6"] and hits[0].kind == CAP


def test_www_prefix_is_same_site():
    hits = run(f=findings(severe_flags=_flag(["a1", "w1"])))
    assert ids(hits) == ["HR-6"]


def test_hr6_single_source_caps():
    assert ids(run(f=findings(severe_flags=_flag(["b1"])))) == ["HR-6"]


@pytest.mark.parametrize("source_ids", [[], ["missing"], ["bad"]])
def test_severe_flag_without_valid_source_is_ignored(source_ids):
    assert run(f=findings(severe_flags=_flag(source_ids))) == []


def test_invalid_source_does_not_count_toward_independence():
    hits = run(f=findings(severe_flags=_flag(["a1", "bad", "missing"])))
    assert ids(hits) == ["HR-6"]


def test_severe_threshold_comes_from_config(hr_config):
    stricter = hr_config.model_copy(update={"severe_negative_min_independent_sources": 3})
    hits = run(f=findings(severe_flags=_flag(["a1", "b1"])), config=stricter)
    assert ids(hits) == ["HR-6"]


# ---------- HR-3 预付费用 ----------


def test_hr3_upfront_payment_rejects():
    hits = run(d=deal(requires_upfront_payment=True))
    assert ids(hits) == ["HR-3"] and hits[0].kind == REJECT


# ---------- HR-4 红灯条款 ----------


def test_hr4_red_term_caps():
    hits = run(d=deal(contract={"kill_fee_percent": None, "revisions": 2}))
    assert ids(hits) == ["HR-4"] and hits[0].kind == CAP
    assert "kill_fee" in hits[0].message


def test_hr4_lists_all_red_terms():
    hits = run(d=deal(contract={"kill_fee_percent": None, "revisions": "unlimited"}))
    assert "kill_fee" in hits[0].message and "revisions" in hits[0].message


def test_yellow_terms_do_not_trigger_hr4():
    assert run(d=deal(contract={"kill_fee_percent": 30, "revisions": 3})) == []


# ---------- HR-5 最低接单价 ----------


@pytest.mark.parametrize(
    "offer, floor, triggered",
    [(249.99, 250, True), (250, 250, False), (100, None, False), (0, 1, True)],
)
def test_hr5_rate_floor(offer, floor, triggered):
    hits = run(p=profile(rate_floor_usd=floor), d=deal(offer_usd=offer))
    assert (ids(hits) == ["HR-5"]) is triggered


# ---------- HR-7 品牌实体 ----------


def test_hr7_unconfirmed_brand_caps():
    hits = run(f=findings(brand_entity_confirmed=False))
    assert ids(hits) == ["HR-7"] and hits[0].kind == CAP


def test_hr7_not_triggered_in_rules_only_mode():
    assert run(f=None) == []


# ---------- 组合 ----------


def test_hits_sorted_by_rule_number():
    hits = run(
        p=profile(rate_floor_usd=1000),
        d=deal(requires_upfront_payment=True, brand_categories=["alcohol"], contract={"revisions": "unlimited"}),
        f=findings(brand_entity_confirmed=False, severe_flags=_flag(["a1", "b1"])),
    )
    assert ids(hits) == ["HR-1", "HR-2", "HR-3", "HR-4", "HR-5", "HR-7"]


# ---------- 证据工具 ----------


@pytest.mark.parametrize(
    "url, domain",
    [
        ("https://www.Example.com/a", "example.com"),
        ("http://news.example.com:8080/x", "news.example.com"),
        ("ftp://example.com", None),
        ("example.com/no-scheme", None),
        ("", None),
    ],
)
def test_domain_of(url, domain):
    assert domain_of(url) == domain


def test_resolve_urls_dedupes_and_drops_invalid():
    f = findings()
    assert resolve_urls(["a1", "a1", "bad", "missing", "b1"], f) == [
        "https://news-a.example/story",
        "https://news-b.example/report",
    ]


def test_independent_source_count():
    assert independent_source_count(["https://a.example/1", "https://www.a.example/2", "https://b.example"]) == 2


# ---------- 扩充后的标准品类（HR-1）----------


NEW_CATEGORIES = ["fast_fashion", "mlm", "supplements_pharma", "political_content", "payday_loans"]


@pytest.mark.parametrize("category", NEW_CATEGORIES)
def test_hr1_new_categories_reject_when_blocked(category):
    hits = run(p=profile(blocked_categories=[category]), d=deal(brand_categories=[category]))
    assert ids(hits) == ["HR-1"] and hits[0].kind == REJECT


@pytest.mark.parametrize("category", NEW_CATEGORIES)
def test_hr1_new_categories_ignored_when_not_blocked(category):
    f = findings(detected_categories=[{"category": category, "source_ids": ["b1"]}])
    assert run(p=profile(blocked_categories=[]), d=deal(brand_categories=[category]), f=f) == []


# ---------- HR-8 自由填写的拒接品类 ----------


def _custom_match(text, source_ids=("b1",)):
    return [{"blocked_text": text, "explanation": "The brand sells energy drinks.", "source_ids": list(source_ids)}]


def test_hr8_custom_match_with_source_caps():
    p = profile(blocked_custom=["Energy drinks"])
    hits = run(p=p, f=findings(custom_category_matches=_custom_match("Energy drinks")))
    assert ids(hits) == ["HR-8"] and hits[0].kind == CAP
    assert "verify it yourself" in hits[0].message
    assert hits[0].evidence_urls == ["https://news-b.example/report"]


def test_hr8_match_ignores_case_and_spaces():
    p = profile(blocked_custom=["Energy drinks"])
    hits = run(p=p, f=findings(custom_category_matches=_custom_match("  energy DRINKS ")))
    assert ids(hits) == ["HR-8"]
    assert '"Energy drinks"' in hits[0].message  # 提示里用创作者自己写的原文


@pytest.mark.parametrize("source_ids", [(), ("missing",), ("bad",)])
def test_hr8_without_valid_source_is_ignored(source_ids):
    p = profile(blocked_custom=["Energy drinks"])
    f = findings(custom_category_matches=_custom_match("Energy drinks", source_ids))
    assert run(p=p, f=f) == []


def test_hr8_ignores_text_not_in_profile():
    """Agent 不能自己编一个拒接理由：只认创作者档案里写了的条目。"""
    p = profile(blocked_custom=["Energy drinks"])
    assert run(p=p, f=findings(custom_category_matches=_custom_match("Sugary snacks"))) == []


def test_hr8_not_triggered_without_custom_items():
    assert run(f=findings(custom_category_matches=_custom_match("Energy drinks"))) == []


def test_hr8_not_triggered_in_rules_only_mode():
    assert run(p=profile(blocked_custom=["Energy drinks"]), f=None) == []


def test_hr8_reported_once_per_custom_item():
    p = profile(blocked_custom=["Energy drinks"])
    f = findings(custom_category_matches=_custom_match("Energy drinks") + _custom_match("energy drinks", ("a1",)))
    assert ids(run(p=p, f=f)) == ["HR-8"]


def test_hr8_min_sources_comes_from_config(hr_config):
    stricter = hr_config.model_copy(update={"custom_category_min_sources": 2})
    p = profile(blocked_custom=["Energy drinks"])
    assert run(p=p, f=findings(custom_category_matches=_custom_match("Energy drinks")), config=stricter) == []


@pytest.mark.parametrize("bad", [[""], ["   "], ["x" * 101], ["item"] * 11])
def test_blocked_custom_validation(bad):
    with pytest.raises(ValidationError):
        profile(blocked_custom=bad)


def test_blocked_custom_is_trimmed():
    assert profile(blocked_custom=["  Energy drinks "]).blocked_custom == ["Energy drinks"]
