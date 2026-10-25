"""DealCheck Agent 后端入口。"""

from fastapi import Depends, FastAPI, HTTPException

from app.models import (
    ContractCheckResult,
    ContractTerms,
    EvaluationRequest,
    EvaluationResult,
    PricingInput,
    PricingResult,
)
from app.config import load_agent_config
from app.llm.base import LLMError
from app.llm.factory import create_llm_provider
from app.pipeline import FullEvaluationRequest, FullEvaluationResult, run_full_evaluation
from app.rules.contract import check_contract
from app.search.base import SearchError
from app.search.factory import create_search_provider
from app.settings import load_settings
from app.rules.engine import evaluate_deal
from app.rules.pricing import UnsupportedFollowerCountError, evaluate_pricing

app = FastAPI(
    title="DealCheck Agent",
    version="0.1.0",
    description="帮创作者评估品牌合作的 AI 助手（M1：规则引擎）",
)


@app.get("/health")
def health() -> dict[str, str]:
    """健康检查：服务正常运行时返回 ok。"""
    return {"status": "ok"}


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


def get_providers():
    """按 .env 创建模型和搜索 Provider；没有 Key 时返回 (None, None, 原因)，评估退回仅规则模式。"""
    settings, config = load_settings(), load_agent_config()
    try:
        return create_llm_provider(settings, config), create_search_provider(settings, config), None
    except (LLMError, SearchError) as e:
        return None, None, str(e)


@app.post("/api/evaluate", response_model=FullEvaluationResult)
def evaluate_full(req: FullEvaluationRequest, providers=Depends(get_providers)) -> FullEvaluationResult:
    """完整评估：规则 + Agent 品牌调查 + 打分决策 + 邮件草稿。Agent 不可用时自动退回仅规则模式。"""
    llm, search, reason = providers
    try:
        result, _ = run_full_evaluation(req, llm, search, unavailable_reason=reason)
    except UnsupportedFollowerCountError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return result
