"""来源登记表：给本次评估搜到的每个网页编号（s1、s2……）。

Agent 只能用编号引用来源、只能读取登记过的网页，因此引用的 URL 一定出现在本次搜索结果里（护栏 G3）。
"""

from app.models import SourceRef
from app.search.base import SearchResult


class SourceRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, SearchResult] = {}
        self._id_by_url: dict[str, str] = {}

    def register(self, result: SearchResult) -> str:
        """登记一条搜索结果并返回编号；同一 URL 重复出现时返回原编号。"""
        if result.url in self._id_by_url:
            return self._id_by_url[result.url]
        sid = f"s{len(self._by_id) + 1}"
        self._by_id[sid] = result
        self._id_by_url[result.url] = sid
        return sid

    def get(self, source_id: str) -> SearchResult | None:
        return self._by_id.get(source_id)

    def __contains__(self, source_id: str) -> bool:
        return source_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def refs(self, source_ids: set[str] | None = None) -> list[SourceRef]:
        """返回来源列表（按编号顺序）；source_ids 不为空时只返回其中登记过的。"""
        return [
            SourceRef(id=sid, url=r.url, title=r.title or None)
            for sid, r in self._by_id.items()
            if source_ids is None or sid in source_ids
        ]
