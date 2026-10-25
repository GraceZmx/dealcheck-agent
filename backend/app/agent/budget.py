"""预算管家（护栏 G8）：数搜索 / 读网页 / 模型调用次数，计时，按用量实时算钱。

所有限制都在程序里强制执行，不依赖模型自觉。成本按高峰时段价格计算（保守），思考 token 按输出计费。
"""

import time
from collections.abc import Callable

from app.config import AgentBudget, ModelPrice, PricingTable
from app.llm.base import Usage


class Budget:
    def __init__(
        self,
        limits: AgentBudget,
        pricing: PricingTable,
        model: str,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limits = limits
        self.pricing = pricing
        self.price = pricing.models.get(model) or self._most_expensive(pricing)
        self._clock = clock
        self._start = clock()
        self.searches = 0
        self.fetches = 0
        self.model_calls = 0
        self.billed_searches = 0
        self.billed_fetches = 0
        self.usage = Usage()

    @staticmethod
    def _most_expensive(pricing: PricingTable) -> ModelPrice:
        """价格表里没有的模型按最贵的价格算，宁可高估成本。"""
        return max(pricing.models.values(), key=lambda p: p.output)

    # ---------- 记录 ----------

    def record_llm(self, usage: Usage) -> None:
        self.model_calls += 1
        self.usage = self.usage + usage

    def record_search(self, billed: bool) -> None:
        self.searches += 1
        self.billed_searches += billed

    def record_fetch(self, billed: bool) -> None:
        self.fetches += 1
        self.billed_fetches += billed

    # ---------- 查询 ----------

    @property
    def searches_left(self) -> int:
        return max(0, self.limits.max_searches - self.searches)

    @property
    def fetches_left(self) -> int:
        return max(0, self.limits.max_fetches - self.fetches)

    @property
    def elapsed_seconds(self) -> float:
        return self._clock() - self._start

    @property
    def cost_usd(self) -> float:
        u, p = self.usage, self.price
        uncached = u.input_tokens - u.cached_input_tokens
        tokens = (uncached * p.input_cache_miss + u.cached_input_tokens * p.input_cache_hit + u.output_tokens * p.output) / 1_000_000
        tools = self.billed_searches * self.pricing.search_per_request_usd + self.billed_fetches * self.pricing.fetch_per_request_usd
        return tokens + tools

    @property
    def cost_cny(self) -> float:
        return self.cost_usd * self.pricing.usd_to_cny

    def can_call_model(self) -> bool:
        return self.model_calls < self.limits.max_model_calls

    def finalize_reason(self) -> str | None:
        """需要立即收尾（下一次调用必须提交结论）时返回原因，否则返回 None。"""
        if self.model_calls >= self.limits.max_model_calls - 1:
            return "model_call_limit"
        if self.elapsed_seconds >= self.limits.max_seconds:
            return "time_limit"
        if self.cost_cny >= self.limits.max_cost_cny:
            return "cost_limit"
        return None

    def summary(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "searches": self.searches,
            "fetches": self.fetches,
            "input_tokens": self.usage.input_tokens,
            "cached_input_tokens": self.usage.cached_input_tokens,
            "output_tokens": self.usage.output_tokens,
            "reasoning_tokens": self.usage.reasoning_tokens,
            "cost_usd": round(self.cost_usd, 5),
            "cost_cny": round(self.cost_cny, 4),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }
