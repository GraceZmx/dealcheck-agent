"""完整评估流程：规则预检 → Agent 调查 → 规则打分决策 → 邮件草稿。

- 预检已命中拒绝类硬性规则（如表单勾选拒接品类、要求预付费用）时跳过 Agent，节省成本；
- Agent 不可用或失败时退回仅规则模式（护栏 G11），结果中注明原因。
"""

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.budget import Budget
from app.agent.email import EmailDraft, draft_email
from app.agent.loop import run_agent
from app.agent.sources import SourceRegistry
from app.agent.tools import ToolContext, tiers_for
from app.config import (
    AgentConfig,
    load_agent_config,
    load_pricing_config,
    load_scoring_config,
)
from app.llm.base import LLMProvider
from app.models import (
    AgentFindings,
    EvaluationRequest,
    EvaluationResult,
    HardRuleKind,
    SourceRef,
)
from app.rules.engine import evaluate_deal
from app.search.base import SearchProvider


class FullEvaluationRequest(EvaluationRequest):
    creator_name: str | None = Field(None, max_length=100, description="只用于在本地填写邮件署名，不发送给模型")


class AgentInfo(BaseModel):
    status: Literal["completed", "fallback", "skipped", "unavailable", "limited"]
    error: str | None = None
    finalize_reason: str | None = None
    model: str | None = None
    usage: dict = Field(default_factory=dict, description="次数、token、成本（人民币 / 美元，含思考 token）、耗时")


class FullEvaluationResult(BaseModel):
    evaluation: EvaluationResult
    email: EmailDraft
    findings: AgentFindings | None
    sources: list[SourceRef]
    agent: AgentInfo


def run_full_evaluation(
    req: FullEvaluationRequest,
    llm: LLMProvider | None,
    search: SearchProvider | None,
    agent_config: AgentConfig | None = None,
    unavailable_reason: str | None = None,
    unavailable_status: Literal["unavailable", "limited"] = "unavailable",
    on_event: Callable[[dict], None] | None = None,
) -> tuple[FullEvaluationResult, list[dict]]:
    """返回评估结果和运行记录（trace）。llm / search 为空时为仅规则模式。

    on_event 依次收到：agent_unavailable / agent_skipped 或 Agent 的各个步骤 → scoring → email。
    """
    emit = on_event or (lambda event: None)
    config = agent_config or load_agent_config()
    pricing_config = load_pricing_config()
    budget = Budget(config.budget, config.pricing, llm.model if llm else config.model.name)

    pre = evaluate_deal(EvaluationRequest(profile=req.profile, deal=req.deal))
    findings, trace = None, []

    if llm is None or search is None:
        agent = AgentInfo(status=unavailable_status, error=unavailable_reason or "Agent is not configured")
        emit({"type": "agent_unavailable", "status": agent.status, "message": agent.error})
    elif any(h.kind == HardRuleKind.REJECT for h in pre.hard_rules):
        agent = AgentInfo(status="skipped", error="A reject-class hard rule already applies; brand research skipped.")
        emit({"type": "agent_skipped", "message": agent.error})
    else:
        ctx = ToolContext(
            profile=req.profile, pricing=pre.pricing, contract=pre.contract, search=search,
            registry=SourceRegistry(), budget=budget, search_settings=config.search,
            tiers=tiers_for(req.profile, pricing_config),
        )
        run = run_agent(req.deal, ctx, llm, config, load_scoring_config(), on_event=emit)
        findings, trace = run.findings, run.trace
        agent = AgentInfo(status=run.status, error=run.error, finalize_reason=run.finalize_reason)

    emit({"type": "scoring"})
    evaluation = (
        evaluate_deal(EvaluationRequest(profile=req.profile, deal=req.deal, agent_findings=findings))
        if findings else pre
    )
    emit({"type": "email"})
    email = draft_email(req.deal, evaluation, llm, config, req.creator_name, on_usage=budget.record_llm)

    agent.model = llm.model if llm else None
    agent.usage = budget.summary()
    result = FullEvaluationResult(
        evaluation=evaluation,
        email=email,
        findings=findings,
        sources=findings.sources if findings else [],
        agent=agent,
    )
    return result, trace
