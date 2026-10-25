"""DealCheck Agent 后端入口：规则接口、完整评估接口（普通 / SSE 实时进度）、防刷保护和前端页面。"""

import json
import queue
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import load_agent_config
from app.llm.base import LLMError
from app.llm.factory import create_llm_provider
from app.models import (
    ContractCheckResult,
    ContractTerms,
    EvaluationRequest,
    EvaluationResult,
    PricingInput,
    PricingResult,
)
from app.pipeline import FullEvaluationRequest, FullEvaluationResult, run_full_evaluation
from app.protection import UsageGuard, client_ip, load_protection_config
from app.rules.contract import check_contract
from app.rules.engine import evaluate_deal
from app.rules.pricing import UnsupportedFollowerCountError, evaluate_pricing
from app.search.base import SearchError
from app.search.factory import create_search_provider
from app.settings import load_settings
from app.traces import save_trace

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
AGENT_OFF_MESSAGE = "AI brand research is temporarily turned off. Showing rules-only results."

app = FastAPI(
    title="DealCheck Agent",
    version="0.3.0",
    description="帮创作者评估品牌合作的 AI 收入决策助手",
)
app.state.guard = UsageGuard(load_protection_config())


def get_guard(request: Request) -> UsageGuard:
    return request.app.state.guard


def get_client_ip(request: Request) -> str:
    c = request.app.state.guard.config
    return client_ip(request.headers, request.client.host if request.client else None, c.trusted_proxy_hops, c.client_ip_header)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """所有 /api 请求按 IP 限制每分钟次数。"""
    if request.url.path.startswith("/api/"):
        if not request.app.state.guard.allow_request(get_client_ip(request)):
            return JSONResponse({"detail": "Too many requests. Please wait a minute and try again."}, status_code=429)
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, str]:
    """健康检查：服务正常运行时返回 ok。"""
    return {"status": "ok"}


# ---------- 规则接口（不调用大模型，不计入 Agent 次数）----------


@app.post("/api/rules/contract-check", response_model=ContractCheckResult)
def contract_check(terms: ContractTerms) -> ContractCheckResult:
    """合同条款红黄绿灯检查（PRD 5.5 节）。每次调用都会重新读取配置文件。"""
    return check_contract(terms)


@app.post("/api/rules/pricing", response_model=PricingResult)
def pricing(inp: PricingInput) -> PricingResult:
    """报价评估（PRD 5.4 节）。每次调用都会重新读取配置和基准表。"""
    try:
        return evaluate_pricing(inp)
    except UnsupportedFollowerCountError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/rules/evaluate", response_model=EvaluationResult)
def evaluate(req: EvaluationRequest) -> EvaluationResult:
    """完整的规则评估：条款 + 报价 + 硬性规则 + 打分 + 决策。agent_findings 为空时为仅规则模式。"""
    try:
        return evaluate_deal(req)
    except UnsupportedFollowerCountError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ---------- 完整评估（Agent）----------


def get_providers():
    """按环境变量创建模型和搜索 Provider；总开关关闭或没有 Key 时返回 (None, None, 原因)。"""
    settings, config = load_settings(), load_agent_config()
    if not settings.agent_enabled:
        return None, None, AGENT_OFF_MESSAGE
    try:
        return create_llm_provider(settings, config), create_search_provider(settings, config), None
    except (LLMError, SearchError) as e:
        return None, None, str(e)


def run_guarded(req: FullEvaluationRequest, providers, guard: UsageGuard, ip: str, on_event=None) -> FullEvaluationResult:
    """先过防刷检查：超出限制时降级为仅规则模式；运行结束后按实际花费结算。"""
    llm, search, reason = providers
    status, ticket = "unavailable", None
    if llm is not None and search is not None:
        ticket, limit = guard.try_start_agent(ip)
        if ticket is None:
            llm, search, reason, status = None, None, guard.message(limit), "limited"
    cost = 0.0
    try:
        result, trace = run_full_evaluation(req, llm, search, unavailable_reason=reason,
                                            unavailable_status=status, on_event=on_event)
        cost = result.agent.usage.get("cost_cny", 0.0)
        if load_settings().save_web_traces:
            try:
                save_trace(result, trace, req.deal.brand_name, tag="web", creator_name=req.creator_name)
            except OSError:
                pass  # 记录写不进去不影响评估结果
        return result
    finally:
        if ticket is not None:
            guard.finish(ticket, cost)


def _validate_followers(req: FullEvaluationRequest) -> None:
    try:
        evaluate_deal(EvaluationRequest(profile=req.profile, deal=req.deal))
    except UnsupportedFollowerCountError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/evaluate", response_model=FullEvaluationResult)
def evaluate_full(
    req: FullEvaluationRequest,
    providers=Depends(get_providers),
    guard: UsageGuard = Depends(get_guard),
    ip: str = Depends(get_client_ip),
) -> FullEvaluationResult:
    """完整评估：规则 + Agent 品牌调查 + 打分决策 + 邮件草稿。Agent 不可用或超出限制时退回仅规则模式。"""
    _validate_followers(req)
    return run_guarded(req, providers, guard, ip)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/evaluate/stream")
def evaluate_stream(
    req: FullEvaluationRequest,
    providers=Depends(get_providers),
    guard: UsageGuard = Depends(get_guard),
    ip: str = Depends(get_client_ip),
):
    """同 /api/evaluate，但用 SSE 实时推送 Agent 的每个真实步骤（progress 事件），最后推送 result 事件。"""
    _validate_followers(req)
    events: queue.Queue = queue.Queue()
    done = object()

    def worker():
        try:
            result = run_guarded(req, providers, guard, ip, on_event=lambda e: events.put(("progress", e)))
            events.put(("result", result.model_dump(mode="json")))
        except Exception as e:  # 推送给前端，由前端回退到普通请求
            events.put(("error", {"message": f"Evaluation failed: {type(e).__name__}"}))
        finally:
            events.put(done)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while True:
            try:
                item = events.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"  # 防止代理因为长时间没有数据而断开连接
                continue
            if item is done:
                return
            yield _sse(*item)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/status")
def status(guard: UsageGuard = Depends(get_guard), ip: str = Depends(get_client_ip)) -> dict:
    """前端用来显示"仅规则模式"或剩余次数的提示。不暴露花费金额。"""
    settings = load_settings()
    agent_ready = settings.agent_enabled and settings.has_deepseek_key
    return {"agent_available": agent_ready, **guard.status(ip)}


@app.get("/api/debug/ip")
def debug_ip(request: Request, ip: str = Depends(get_client_ip)) -> dict:
    """部署诊断：只有环境变量 DEBUG_IP=true 时可用，返回调用者自己的 IP 相关请求头，用于确定真实 IP 的来源。"""
    if not load_settings().debug_ip:
        raise HTTPException(status_code=404, detail="Not Found")
    h = request.headers
    return {"detected": ip, "peer": request.client.host if request.client else None,
            "x_forwarded_for": h.get("x-forwarded-for"), "true_client_ip": h.get("true-client-ip"),
            "cf_connecting_ip": h.get("cf-connecting-ip"), "x_real_ip": h.get("x-real-ip")}


# 前端页面放在最后挂载，避免覆盖上面的接口
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
