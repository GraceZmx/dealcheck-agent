"""DealCheck Agent 后端入口。"""

from fastapi import FastAPI, HTTPException

from app.models import (
    ContractCheckResult,
    ContractTerms,
    EvaluationRequest,
    EvaluationResult,
    PricingInput,
    PricingResult,
)
from app.rules.contract import check_contract
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
