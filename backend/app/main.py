"""DealCheck Agent 后端入口。"""

from fastapi import FastAPI

from app.models import ContractCheckResult, ContractTerms
from app.rules.contract import check_contract

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
