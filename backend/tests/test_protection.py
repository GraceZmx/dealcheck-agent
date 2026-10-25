"""防刷保护测试：IP 每日次数、全站花费上限、请求频率、总开关、代理 IP 解析，以及 SSE 实时进度接口。"""

import json

import pytest
from fastapi.testclient import TestClient

from app.llm.mock import MockProvider, assistant, tool_call
from app.main import app, get_providers
from app.protection import ProtectionConfig, UsageGuard, client_ip, load_protection_config
from app.settings import load_settings
from tests.test_pipeline import SUBMIT, agent_script, request, search


def config(**overrides) -> ProtectionConfig:
    base = load_protection_config().model_dump()
    base.update(overrides)
    return ProtectionConfig.model_validate(base)


class Clock:
    def __init__(self):
        self.now, self.day = 0.0, "2026-10-25"

    def time(self):
        return self.now

    def today(self):
        return self.day


def guard(clock=None, **overrides) -> UsageGuard:
    clock = clock or Clock()
    return UsageGuard(config(**overrides), clock=clock.time, today=clock.today)


# ---------- 每个 IP 每天的 Agent 次数 ----------


def test_default_limits():
    c = load_protection_config()
    assert (c.per_ip_agent_evaluations_per_day, c.global_daily_cost_cny, c.per_ip_requests_per_minute) == (5, 5.0, 30)


def test_ip_daily_limit():
    g = guard()
    for _ in range(5):
        ticket, reason = g.try_start_agent("1.1.1.1")
        assert ticket and reason is None
        g.finish(ticket, 0.04)
    assert g.try_start_agent("1.1.1.1") == (None, "ip_daily_limit")
    assert g.try_start_agent("2.2.2.2")[0] is not None  # 其他 IP 不受影响


def test_limits_reset_next_day():
    clock = Clock()
    g = guard(clock, per_ip_agent_evaluations_per_day=1)
    g.finish(g.try_start_agent("1.1.1.1")[0], 0.04)
    assert g.try_start_agent("1.1.1.1")[1] == "ip_daily_limit"
    clock.day = "2026-10-26"
    assert g.try_start_agent("1.1.1.1")[0] is not None
    assert g.spent_today_cny == 0


# ---------- 全站每天的花费上限 ----------


def test_global_budget_uses_actual_cost():
    g = guard(global_daily_cost_cny=1.0, per_ip_agent_evaluations_per_day=100)
    for i in range(10):  # 每次实际 ¥0.05，合计 ¥0.50
        g.finish(g.try_start_agent(f"10.0.0.{i}")[0], 0.05)
    assert g.try_start_agent("10.0.1.1")[0] is not None  # 0.50 + 预留 0.50 = 1.00，刚好不超
    assert g.try_start_agent("10.0.1.2") == (None, "global_daily_budget")


def test_reservation_blocks_concurrent_burst():
    """并发请求还没结算时，按单次上限预留，不会一起冲过全站上限。"""
    g = guard(global_daily_cost_cny=1.0, per_ip_agent_evaluations_per_day=100)
    t1, _ = g.try_start_agent("a")
    t2, _ = g.try_start_agent("b")
    assert t1 and t2
    assert g.try_start_agent("c") == (None, "global_daily_budget")
    g.finish(t1, 0.04)  # 已花 0.04 + t2 预留 0.50 + 新预留 0.50 = 1.04 > 1.00
    assert g.try_start_agent("c") == (None, "global_daily_budget")
    g.finish(t2, 0.04)  # 已花 0.08 + 新预留 0.50 = 0.58
    assert g.try_start_agent("c")[0] is not None


def test_ticket_from_previous_day_not_counted():
    clock = Clock()
    g = guard(clock)
    ticket, _ = g.try_start_agent("a")
    clock.day = "2026-10-26"
    g.finish(ticket, 0.4)
    assert g.spent_today_cny == 0


def test_status_and_messages():
    g = guard(per_ip_agent_evaluations_per_day=2)
    g.finish(g.try_start_agent("a")[0], 0.04)
    assert g.status("a") == {"remaining_today": 1, "daily_limit": 2, "site_budget_available": True}
    assert "limit of 2 AI evaluations" in g.message("ip_daily_limit")
    assert "daily AI budget" in g.message("global_daily_budget")


# ---------- 请求频率 ----------


def test_requests_per_minute_sliding_window():
    clock = Clock()
    g = guard(clock, per_ip_requests_per_minute=3)
    assert all(g.allow_request("a") for _ in range(3))
    assert not g.allow_request("a")
    assert g.allow_request("b")
    clock.now = 59.9
    assert not g.allow_request("a")
    clock.now = 60.0
    assert g.allow_request("a")


# ---------- 代理后的真实 IP ----------


@pytest.mark.parametrize(
    "header, hops, expected",
    [
        ("203.0.113.5", 1, "203.0.113.5"),
        ("6.6.6.6, 203.0.113.5", 1, "203.0.113.5"),  # 客户端伪造的值在左边，被忽略
        ("6.6.6.6, 203.0.113.5, 10.0.0.2", 2, "203.0.113.5"),
        (None, 1, "127.0.0.9"),
        ("203.0.113.5", 0, "127.0.0.9"),  # 本地开发不信任该请求头
    ],
)
def test_client_ip(header, hops, expected):
    headers = {"x-forwarded-for": header} if header else {}
    assert client_ip(headers, "127.0.0.9", hops) == expected


# ---------- 接口集成 ----------


@pytest.fixture
def fresh_guard():
    original = app.state.guard
    app.state.guard = UsageGuard(config(per_ip_agent_evaluations_per_day=1, trusted_proxy_hops=1))
    yield app.state.guard
    app.state.guard = original
    app.dependency_overrides.clear()


def _body():
    return request().model_dump(mode="json")


def test_second_agent_evaluation_from_same_ip_is_rules_only(fresh_guard):
    app.dependency_overrides[get_providers] = lambda: (MockProvider(agent_script()), search(), None)
    client = TestClient(app)
    headers = {"x-forwarded-for": "198.51.100.7"}
    first = client.post("/api/evaluate", json=_body(), headers=headers).json()
    second = client.post("/api/evaluate", json=_body(), headers=headers).json()
    assert first["agent"]["status"] == "completed"
    assert second["agent"]["status"] == "limited"
    assert "limit of 1 AI evaluations" in second["agent"]["error"]
    assert second["evaluation"]["decision"] == "negotiate"  # 规则结果照常返回
    assert fresh_guard.spent_today_cny > 0


def test_rules_endpoints_not_counted_as_agent(fresh_guard):
    client = TestClient(app)
    raw = _body()
    raw.pop("creator_name", None)
    for _ in range(3):
        assert client.post("/api/rules/evaluate", json=raw).status_code == 200
    assert fresh_guard.status("testclient")["remaining_today"] == 1


def test_rate_limit_returns_429(fresh_guard):
    fresh_guard.config = config(per_ip_requests_per_minute=2, trusted_proxy_hops=1)
    client = TestClient(app)
    codes = [client.get("/api/status").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert client.get("/health").status_code == 200  # 健康检查不限流


def test_agent_switch_off(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "false")
    assert load_settings().agent_enabled is False
    llm, srch, reason = get_providers()
    assert llm is None and "turned off" in reason


def test_status_endpoint(fresh_guard):
    body = TestClient(app).get("/api/status").json()
    assert {"agent_available", "remaining_today", "daily_limit", "site_budget_available"} <= set(body)
    assert "spent" not in json.dumps(body)


# ---------- SSE 实时进度 ----------


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = [l for l in block.splitlines() if not l.startswith(":")]
        if not lines:
            continue
        event = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((event, data))
    return events


def test_stream_reports_real_steps_then_result(fresh_guard):
    script = [
        assistant("", tool_call("web_search", json.dumps({"query": "glow theory influencer payment"}))),
        assistant("", tool_call("fetch_page", json.dumps({"source_id": "s2"}))),
        assistant("", tool_call("submit_findings", json.dumps(SUBMIT))),
        assistant(json.dumps({"subject": "Re", "body": "{{counter_rate}} {{ask_1}} {{ask_2}}\n{{creator_name}}"})),
    ]
    app.dependency_overrides[get_providers] = lambda: (MockProvider(script), search(), None)
    with TestClient(app).stream("POST", "/api/evaluate/stream", json=_body()) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(response.read().decode())

    progress = [d for e, d in events if e == "progress"]
    assert [p["type"] for p in progress] == [
        "thinking", "search", "thinking", "read", "thinking", "submitting", "scoring", "email"]
    assert progress[1]["query"] == "glow theory influencer payment"
    assert progress[3]["source"]["domain"] == "site1.example"
    final = [d for e, d in events if e == "result"]
    assert len(final) == 1 and final[0]["evaluation"]["decision"] == "negotiate"
    assert events[-1][0] == "result"


def test_stream_reports_limited_mode(fresh_guard):
    fresh_guard.config = config(per_ip_agent_evaluations_per_day=0)
    app.dependency_overrides[get_providers] = lambda: (MockProvider([]), search(), None)
    with TestClient(app).stream("POST", "/api/evaluate/stream", json=_body()) as response:
        events = parse_sse(response.read().decode())
    assert events[0] == ("progress", {"type": "agent_unavailable", "status": "limited",
                                       "message": fresh_guard.message("ip_daily_limit")})
    assert events[-1][1]["agent"]["status"] == "limited"


def test_stream_rejects_unsupported_followers(fresh_guard):
    raw = _body()
    raw["profile"]["followers"]["instagram"] = 700_000
    assert TestClient(app).post("/api/evaluate/stream", json=raw).status_code == 422
