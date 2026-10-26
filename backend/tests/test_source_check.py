"""来源支持度检查测试：取结论、取来源原文、拼评审请求、解析和汇总。不调用模型。只用虚构品牌。"""

import json

import pytest

from app.source_check import (
    build_judge_messages,
    extract_claims,
    merge_verdicts,
    parse_verdicts,
    source_texts,
    split_claims,
    summarize_support,
)

HYBRID = {
    "findings": {
        "brand_overview": {
            "who_they_are": "Fernwick Loop sells bike lights (s1). It is based in Leeds.",
            "working_with_creators": "",
            "regulatory_and_controversies": "A 2024 recall covered one model (s2, s3).",
        },
        "values_fit": {"level": "high", "summary": "Uses recycled packaging.", "source_ids": ["s1"]},
        "severe_flags": [{"type": "safety", "description": "Recall of a light", "source_ids": ["s2"]}],
        "detected_categories": [],
        "custom_category_matches": [],
        "peer_rates": [],
        "score_adjustments": [{"item": "brand_reputation", "delta": -2, "reason": "Late payments (s9).", "source_ids": []}],
    }
}

TRACE = [
    {"type": "llm"},
    {"type": "tool", "name": "web_search", "result": json.dumps({"results": [
        {"source_id": "s1", "url": "https://a.example", "title": "A", "summary": "Bike light maker."},
        {"source_id": "s2", "url": "https://b.example", "title": "B", "summary": "Recall notice."},
    ]})},
    {"type": "tool", "name": "fetch_page", "result": json.dumps({"source_id": "s2", "content": "Full recall text."})},
    {"type": "tool", "name": "fetch_page", "result": json.dumps({"error": "fetch_failed", "source_id": "s1"})},
]


def test_extract_claims_only_cited_sentences():
    claims = extract_claims(HYBRID)
    texts = [c["text"] for c in claims]
    assert "It is based in Leeds." not in texts  # 没有引用，不算
    assert claims[0] == {"id": "c1", "where": "overview.who_they_are",
                         "text": "Fernwick Loop sells bike lights (s1).", "source_ids": ["s1"]}
    assert claims[1]["source_ids"] == ["s2", "s3"]
    assert {c["where"] for c in claims} >= {"values_fit", "severe_flags[0]", "score_adjustments[0]"}


def test_extract_claims_agent_only_reasons():
    claims = extract_claims({"findings": {"reasons": ["Fair offer.", "Good reviews (s4)."], "severe_flags": []}})
    assert [(c["where"], c["source_ids"]) for c in claims] == [("reasons[1]", ["s4"])]


def test_no_findings_no_claims():
    assert extract_claims({"findings": None}) == []


def test_source_texts_merges_summary_and_page():
    sources = source_texts(TRACE)
    assert set(sources) == {"s1", "s2"}
    assert sources["s2"]["text"].startswith("Recall notice.") and "Full recall text." in sources["s2"]["text"]
    assert sources["s1"]["text"] == "Bike light maker."  # 读取失败的网页不追加


def test_split_claims_no_source():
    claims = extract_claims(HYBRID)
    judged, no_source = split_claims(claims, source_texts(TRACE))
    assert [c["text"] for c in no_source] == ["[brand_reputation -2] Late payments (s9)."]
    assert all(c not in no_source for c in judged)


def test_judge_messages_only_cited_sources():
    sources = {**source_texts(TRACE), "s5": {"url": "https://c.example", "title": "C", "text": "unrelated"}}
    claims = [{"id": "c1", "text": "x (s1)", "source_ids": ["s1"]}]
    user = build_judge_messages(claims, sources)[1].content
    assert "### s1" in user and "### s5" not in user


def test_parse_and_merge_verdicts():
    claims = [{"id": "c1", "text": "a", "source_ids": ["s1"]}, {"id": "c2", "text": "b", "source_ids": ["s2"]}]
    content = json.dumps({"verdicts": [{"claim_id": "c1", "verdict": "supported", "reason": "ok"},
                                       {"claim_id": "c2", "verdict": "partial", "reason": "date wrong"}]})
    merged = merge_verdicts(claims, [{"id": "c3", "text": "c", "source_ids": ["s9"]}], parse_verdicts(content, claims))
    assert [c["verdict"] for c in merged] == ["supported", "partial", "no_source"]


def test_parse_verdicts_rejects_missing():
    claims = [{"id": "c1"}, {"id": "c2"}]
    with pytest.raises(ValueError, match="c2"):
        parse_verdicts(json.dumps({"verdicts": [{"claim_id": "c1", "verdict": "supported"}]}), claims)


def test_summarize_support():
    runs = [[{"verdict": "supported"}, {"verdict": "partial"}], [{"verdict": "no_source"}, {"verdict": "supported"}]]
    s = summarize_support(runs)
    assert s == {"claims": 4, "supported": 2, "partial": 1, "unsupported": 0, "no_source": 1, "support_rate": 0.5}
    assert summarize_support([])["support_rate"] is None
