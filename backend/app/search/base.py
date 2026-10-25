"""搜索封装层的统一接口：search() 找网页，fetch() 读单个网页全文。"""

from abc import ABC, abstractmethod

from pydantic import BaseModel


class SearchResult(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""
    summary: str = ""
    date: str | None = None
    authority: int | None = None  # 0–4，-1 表示未知
    rerank_score: float | None = None
    site_name: str | None = None


class FetchResult(BaseModel):
    url: str
    title: str = ""
    content: str = ""


class SearchError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class SearchProvider(ABC):
    name: str

    @abstractmethod
    def search(self, query: str, count: int) -> list[SearchResult]:
        """搜索网页。没有结果时返回空列表。"""

    @abstractmethod
    def fetch(self, url: str) -> FetchResult:
        """读取单个网页，返回 Markdown 正文。读不到时抛出 SearchError。"""
