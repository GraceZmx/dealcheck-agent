"""线上防刷保护：IP 限流、每个 IP 每天的 Agent 次数、全站每天的花费上限。

计数存在内存里（单实例部署），按 UTC 日期每天清零；服务重启也会清零。
超出 Agent 限制时不报错，而是降级为仅规则模式；超出请求频率时返回 429。
"""

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from app.config import CONFIG_DIR, load_yaml

LIMIT_MESSAGES = {
    "ip_daily_limit": "You've reached today's limit of {n} AI evaluations. Showing rules-only results; try again tomorrow.",
    "global_daily_budget": "The site's daily AI budget has been used up. Showing rules-only results; try again tomorrow.",
}


class ProtectionConfig(BaseModel):
    per_ip_agent_evaluations_per_day: int = Field(ge=0)
    global_daily_cost_cny: float = Field(ge=0)
    per_ip_requests_per_minute: int = Field(ge=1)
    reserve_cost_cny_per_evaluation: float = Field(ge=0)
    trusted_proxy_hops: int = Field(ge=0)
    client_ip_header: str = ""


def load_protection_config(path: Path | None = None) -> ProtectionConfig:
    return ProtectionConfig.model_validate(load_yaml(path or CONFIG_DIR / "protection.yaml"))


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@dataclass(frozen=True)
class Ticket:
    ip: str
    day: str
    reserved_cny: float


class UsageGuard:
    def __init__(
        self,
        config: ProtectionConfig,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], str] = utc_today,
    ):
        self.config = config
        self._clock = clock
        self._today = today
        self._lock = threading.Lock()
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._day = today()
        self._agent_counts: dict[str, int] = defaultdict(int)
        self._spent_cny = 0.0
        self._reserved_cny = 0.0

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._agent_counts.clear()
            self._spent_cny = 0.0
            self._reserved_cny = 0.0

    # ---------- 请求频率 ----------

    def allow_request(self, ip: str) -> bool:
        """滑动窗口：过去 60 秒内的请求数不超过上限。"""
        now = self._clock()
        with self._lock:
            window = self._requests[ip]
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= self.config.per_ip_requests_per_minute:
                return False
            window.append(now)
            if len(self._requests) > 10_000:  # 防止大量不同 IP 让内存无限增长
                for key in [k for k, v in self._requests.items() if not v]:
                    del self._requests[key]
            return True

    # ---------- Agent 次数与花费 ----------

    def try_start_agent(self, ip: str) -> tuple[Ticket | None, str | None]:
        """允许运行 Agent 时返回 (ticket, None)；否则返回 (None, 原因代码)。"""
        c = self.config
        with self._lock:
            self._roll_day()
            if self._agent_counts[ip] >= c.per_ip_agent_evaluations_per_day:
                return None, "ip_daily_limit"
            if self._spent_cny + self._reserved_cny + c.reserve_cost_cny_per_evaluation > c.global_daily_cost_cny:
                return None, "global_daily_budget"
            self._agent_counts[ip] += 1
            self._reserved_cny += c.reserve_cost_cny_per_evaluation
            return Ticket(ip=ip, day=self._day, reserved_cny=c.reserve_cost_cny_per_evaluation), None

    def finish(self, ticket: Ticket, actual_cost_cny: float) -> None:
        """评估结束：释放预留，记入实际花费。跨天结束的评估不再计入新的一天。"""
        with self._lock:
            self._roll_day()
            if ticket.day != self._day:
                return
            self._reserved_cny = max(0.0, self._reserved_cny - ticket.reserved_cny)
            self._spent_cny += actual_cost_cny

    def message(self, reason: str) -> str:
        return LIMIT_MESSAGES[reason].format(n=self.config.per_ip_agent_evaluations_per_day)

    def status(self, ip: str) -> dict:
        c = self.config
        with self._lock:
            self._roll_day()
            remaining = max(0, c.per_ip_agent_evaluations_per_day - self._agent_counts[ip])
            budget_ok = self._spent_cny + self._reserved_cny + c.reserve_cost_cny_per_evaluation <= c.global_daily_cost_cny
            return {"remaining_today": remaining, "daily_limit": c.per_ip_agent_evaluations_per_day,
                    "site_budget_available": budget_ok}

    @property
    def spent_today_cny(self) -> float:
        with self._lock:
            self._roll_day()
            return self._spent_cny


def client_ip(headers, peer: str | None, trusted_proxy_hops: int, ip_header: str = "") -> str:
    """部署在代理后面时，取 X-Forwarded-For 右数第 trusted_proxy_hops 个地址：
    代理会把它看到的客户端地址追加在最右边，客户端自己伪造的值只能出现在更左边。
    配置了 ip_header 时优先读取该请求头（由代理覆盖写入的请求头，如 true-client-ip）。"""
    if ip_header and headers.get(ip_header):
        return headers.get(ip_header).split(",")[0].strip()
    forwarded = headers.get("x-forwarded-for")
    if trusted_proxy_hops and forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-trusted_proxy_hops] if len(parts) >= trusted_proxy_hops else parts[0]
    return peer or "unknown"
