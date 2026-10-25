"""Agent 证据的基础校验：来源 ID 是否存在、URL 是否有效、来源是否独立。

M3 的护栏层还会校验 URL 是否真的出现在本次搜索结果里（G3）；这里只做规则层能独立判断的部分。
"""

from urllib.parse import urlparse

from app.models import AgentFindings


def resolve_urls(source_ids: list[str], findings: AgentFindings) -> list[str]:
    """把来源 ID 换成 URL，丢弃不存在的 ID 和无效 URL，去重并保持顺序。"""
    sources = findings.source_map
    urls: list[str] = []
    for sid in source_ids:
        src = sources.get(sid)
        if src and domain_of(src.url) and src.url not in urls:
            urls.append(src.url)
    return urls


def domain_of(url: str) -> str | None:
    """取网站域名用于判断来源是否独立；www. 前缀忽略。无效 URL 返回 None。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    return host.removeprefix("www.")


def independent_source_count(urls: list[str]) -> int:
    return len({domain_of(u) for u in urls} - {None})
