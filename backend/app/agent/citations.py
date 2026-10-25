"""护栏 G12：引用一致性。

Badcase BC-1（M2 本地实测）：品牌摘要正文里引用了 (s12)，但来源列表只统计结构化字段（调分、严重负面等）里的编号，
s12 没被收录，用户看到一个找不到出处的角标。

规则：Agent 结论里出现的每个来源编号（正文中的引用和 source_ids 字段）都必须出现在最终来源列表里。
- 本次搜索中确实登记过的编号 → 补进来源列表；
- 不存在的编号 → 从正文和 source_ids 中删除，正文里替换为 "unverified"，并记录在 unverified_citations 中。
"""

import re

from app.agent.sources import SourceRegistry
from app.models import AgentFindings, AgentSubmission

CITATION = re.compile(r"\bs\d+\b")
UNVERIFIED = "unverified"


def _clean_text(text: str, registry: SourceRegistry, cited: set[str], unverified: set[str]) -> str:
    def replace(match: re.Match) -> str:
        sid = match.group(0)
        if sid in registry:
            cited.add(sid)
            return sid
        unverified.add(sid)
        return UNVERIFIED

    return CITATION.sub(replace, text)


def _clean_ids(ids: list[str], registry: SourceRegistry, cited: set[str], unverified: set[str]) -> list[str]:
    kept = []
    for sid in ids:
        if sid in registry:
            cited.add(sid)
            if sid not in kept:
                kept.append(sid)
        else:
            unverified.add(sid)
    return kept


def enforce_citations(submission: AgentSubmission, registry: SourceRegistry) -> AgentFindings:
    """把 Agent 的提交整理成结论：清理引用，并按实际引用生成来源列表。"""
    cited: set[str] = set()
    unverified: set[str] = set()
    s = submission.model_copy(deep=True)

    def text(value: str) -> str:
        return _clean_text(value, registry, cited, unverified)

    def ids(value: list[str]) -> list[str]:
        return _clean_ids(value, registry, cited, unverified)

    o = s.brand_overview
    o.who_they_are = text(o.who_they_are)
    o.working_with_creators = text(o.working_with_creators)
    o.regulatory_and_controversies = text(o.regulatory_and_controversies)
    s.values_fit.summary = text(s.values_fit.summary)
    s.values_fit.source_ids = ids(s.values_fit.source_ids)
    for f in s.severe_flags:
        f.description, f.source_ids = text(f.description), ids(f.source_ids)
    for d in s.detected_categories:
        d.source_ids = ids(d.source_ids)
    for m in s.custom_category_matches:
        m.explanation, m.source_ids = text(m.explanation), ids(m.source_ids)
    for p in s.peer_rates:
        p.description, p.source_ids = text(p.description), ids(p.source_ids)
    for a in s.score_adjustments:
        a.reason, a.source_ids = text(a.reason), ids(a.source_ids)

    def order(sid: str) -> int:
        return int(sid[1:])

    return AgentFindings(
        **s.model_dump(),
        sources=registry.refs(cited),
        unverified_citations=sorted(unverified, key=order),
    )


def all_citations(findings: AgentFindings) -> set[str]:
    """结论里出现的全部编号（正文 + source_ids），用于校验 G12。"""
    texts = [text for _, text in findings.brand_overview.sections()] + [findings.values_fit.summary]
    texts += [f.description for f in findings.severe_flags]
    texts += [m.explanation for m in findings.custom_category_matches]
    texts += [p.description for p in findings.peer_rates]
    texts += [a.reason for a in findings.score_adjustments]
    found = {sid for t in texts for sid in CITATION.findall(t)}
    groups = [findings.severe_flags, findings.detected_categories, findings.custom_category_matches,
              findings.peer_rates, findings.score_adjustments]
    found |= set(findings.values_fit.source_ids)
    for group in groups:
        for item in group:
            found |= set(item.source_ids)
    return found
