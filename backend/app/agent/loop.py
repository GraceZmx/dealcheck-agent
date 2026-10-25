"""Agent Loop：想一步 → 调工具 → 看结果 → 再想，直到 Agent 调用 submit_findings 提交结论。

收尾机制：模型调用次数、时间或成本到上限时，下一次调用只提供 submit_findings，并要求立即提交。
失败处理：结论格式错误允许重试（次数见配置）；模型报错或始终没有提交时，返回 fallback，由上层退回仅规则模式。
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from app.agent.citations import enforce_citations
from app.agent.prompts import build_system_prompt, build_task_message
from app.agent.tools import SUBMIT_SPEC, TOOL_SPECS, ToolContext, execute_tool
from app.agent.urls import clean_url
from app.rules.evidence import domain_of
from app.config import AgentConfig, ScoringConfig
from app.llm.base import ChatOptions, LLMError, LLMProvider, Message
from app.models import AgentFindings, AgentSubmission, DealInput

NUDGE_SUBMIT = "Call submit_findings to submit your findings. If you found nothing reliable, report insufficient information."
FINALIZE = "Budget limit reached ({reason}). Call submit_findings now using the information you already have."


@dataclass
class AgentRun:
    status: Literal["completed", "fallback"]
    findings: AgentFindings | None = None
    error: str | None = None
    finalize_reason: str | None = None
    trace: list[dict] = field(default_factory=list)
    budget: dict = field(default_factory=dict)


def to_findings(submission: AgentSubmission, ctx: ToolContext) -> AgentFindings:
    """来源列表由程序根据登记表生成（G3），并保证正文和字段里的每个引用都在列表中（G12）。"""
    return enforce_citations(submission, ctx.registry)


def _preview(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def describe_tool_call(name: str, arguments: str, ctx: ToolContext) -> dict:
    """把一次工具调用描述成给用户看的进度事件（只含真实发生的动作，不含网页内容）。"""
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    if name == "web_search":
        return {"type": "search", "query": str(args.get("query", ""))[:128]}
    if name == "fetch_page":
        source = ctx.registry.get(str(args.get("source_id", "")))
        if source is None:
            return {"type": "read", "source": None}
        return {"type": "read", "source": {"title": source.title, "domain": domain_of(clean_url(source.url))}}
    return {"type": "tool", "name": name}


def run_agent(
    deal: DealInput,
    ctx: ToolContext,
    provider: LLMProvider,
    config: AgentConfig,
    scoring: ScoringConfig,
    on_event: Callable[[dict], None] | None = None,
) -> AgentRun:
    """on_event：每个真实步骤发生时回调一次（用于界面上的实时进度），不影响 Agent 行为。"""
    budget = ctx.budget
    emit = on_event or (lambda event: None)
    options = ChatOptions(
        max_tokens=config.model.max_tokens,
        temperature=config.model.temperature,
        thinking=config.model.thinking,
        reasoning_effort=config.model.reasoning_effort,
    )
    messages = [
        Message(role="system", content=build_system_prompt(config.budget, scoring)),
        Message(role="user", content=build_task_message(deal, ctx)),
    ]
    trace: list[dict] = []
    format_failures = 0
    finalize_reason: str | None = None

    def done(status, findings=None, error=None) -> AgentRun:
        return AgentRun(status=status, findings=findings, error=error, finalize_reason=finalize_reason,
                        trace=trace, budget=budget.summary())

    while budget.can_call_model():
        reason = budget.finalize_reason()
        if reason and finalize_reason is None:
            finalize_reason = reason
            messages.append(Message(role="user", content=FINALIZE.format(reason=reason)))
        final_mode = finalize_reason is not None
        tools = [SUBMIT_SPEC] if final_mode else [*TOOL_SPECS, SUBMIT_SPEC]
        emit({"type": "thinking", "final": final_mode})

        try:
            response = provider.chat(messages, tools, options)
        except LLMError as e:
            trace.append({"type": "llm_error", "error": str(e)})
            return done("fallback", error=str(e))

        budget.record_llm(response.usage)
        msg = response.message
        messages.append(msg)
        trace.append({
            "type": "llm",
            "call": budget.model_calls,
            "final_mode": final_mode,
            "latency_seconds": response.latency_seconds,
            "usage": response.usage.model_dump(),
            "tool_calls": [tc.name for tc in msg.tool_calls],
        })

        if not msg.tool_calls:
            messages.append(Message(role="user", content=NUDGE_SUBMIT))
            continue

        submission: AgentSubmission | None = None
        for tc in msg.tool_calls:
            if tc.name == SUBMIT_SPEC.name:
                emit({"type": "submitting"})
                try:
                    submission = AgentSubmission.model_validate_json(tc.arguments)
                    result = json.dumps({"status": "received"})
                except ValidationError as e:
                    format_failures += 1
                    errors = [{"loc": list(err["loc"]), "msg": err["msg"]} for err in e.errors()[:10]]
                    result = json.dumps({"error": "invalid_findings", "details": errors,
                                         "message": "Fix these fields and call submit_findings again."})
                    trace.append({"type": "invalid_submission", "errors": errors})
            elif final_mode:
                result = json.dumps({"error": "finalize_now", "message": "Only submit_findings is allowed now."})
            else:
                emit(describe_tool_call(tc.name, tc.arguments, ctx))
                result = execute_tool(ctx, tc)
                trace.append({"type": "tool", "name": tc.name, "arguments": tc.arguments, "result": _preview(result)})
            messages.append(Message(role="tool", tool_call_id=tc.id, content=result))

        if submission is not None:
            return done("completed", findings=to_findings(submission, ctx))
        if format_failures > config.budget.max_format_retries:
            return done("fallback", error="Agent findings failed validation after retry")

    return done("fallback", error="Model call limit reached without findings")
