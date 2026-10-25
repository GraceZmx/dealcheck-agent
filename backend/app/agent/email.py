"""谈判邮件草稿（PRD 5.10 节）。只起草，不发送。

数字不经过模型：模型只能写占位符（如 {{counter_rate}}），由程序填入规则引擎算出的值；
署名 {{creator_name}} 在本地填入，创作者姓名不发送给模型。
模型输出不合格（缺占位符、用了未知占位符、自己写了金额 / 百分比 / 天数）或调用失败时，改用模板邮件。
"""

import json
import re
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.config import AgentConfig
from app.llm.base import ChatOptions, LLMError, LLMProvider, Message
from app.models import Decision, DealInput, EvaluationResult, Light

PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")
# 模型自己写出的金额、百分比或天数 / 月数：这些数字只能来自占位符
FORBIDDEN_NUMBERS = re.compile(r"\$\s?\d|\d\s?%|\b\d+\s?(?:day|days|month|months|week|weeks)\b", re.IGNORECASE)
DEFAULT_SIGNATURE = "[Your name]"

EMAIL_SYSTEM_PROMPT = """You write short, professional reply emails from a content creator to a brand about a sponsorship offer.

Write the email in English. Tone: warm, confident, concise (under 180 words). Never apologetic, never pushy.

You must use placeholders instead of any numbers. Allowed placeholders:
{allowed}
Never write dollar amounts, percentages, or day/month counts yourself; only use the placeholders above.
Never mention scores, benchmarks, internal analysis, or research about the brand.
End the email with the sign-off and {{{{creator_name}}}}.

Return JSON: {{"subject": "...", "body": "..."}}"""

GOALS = {
    Decision.ACCEPT: "Accept the offer and confirm the agreed terms. Ask for the contract or brief to get started.",
    Decision.NEGOTIATE: "Express interest, then ask for the changes listed below. Keep the door open.",
    Decision.DECLINE: "Politely decline. Give a brief, non-specific reason (for example, not the right fit right now). "
                      "Do not mention concerns about the brand.",
}


class EmailDraft(BaseModel):
    subject: str
    body: str
    generated_by: Literal["llm", "template"]
    note: str | None = None


def build_variables(deal: DealInput, evaluation: EvaluationResult) -> dict[str, str]:
    """占位符的值，全部来自用户输入或规则引擎。"""
    values = {"brand_name": deal.brand_name, "offer": f"${deal.offer_usd:,.0f}"}
    if evaluation.decision != Decision.NEGOTIATE:
        return values
    counter = evaluation.pricing.suggested_counter_range
    if counter:
        values["counter_rate"] = f"${round(counter.high / 10) * 10:,.0f}"
    asks = [t.suggestion for t in evaluation.contract.terms if t.light != Light.GREEN and t.suggestion]
    for i, ask in enumerate(asks, start=1):
        values[f"ask_{i}"] = ask
    return values


def _describe(variables: dict[str, str]) -> str:
    lines = ["- {{creator_name}}: the creator's name (sign-off)"]
    for name, value in variables.items():
        hint = {"brand_name": "the brand's name", "offer": "the brand's offered fee",
                "counter_rate": "the creator's counter-offer rate"}.get(name, "a requested contract change")
        lines.append(f"- {{{{{name}}}}}: {hint}" + (f' (reads: "{value}")' if name.startswith("ask_") else ""))
    return "\n".join(lines)


def required_placeholders(variables: dict[str, str]) -> set[str]:
    return {"creator_name"} | {k for k in variables if k == "counter_rate" or k.startswith("ask_")}


def validate_and_fill(subject: str, body: str, variables: dict[str, str], creator_name: str | None) -> tuple[str, str]:
    """检查占位符和数字，通过后填入真实值。不通过时抛出 ValueError。"""
    allowed = set(variables) | {"creator_name"}
    used = set(PLACEHOLDER.findall(subject + body))
    if unknown := used - allowed:
        raise ValueError(f"unknown placeholders: {sorted(unknown)}")
    if missing := required_placeholders(variables) - set(PLACEHOLDER.findall(body)):
        raise ValueError(f"missing placeholders: {sorted(missing)}")
    if FORBIDDEN_NUMBERS.search(PLACEHOLDER.sub("", subject + " " + body)):
        raise ValueError("email contains numbers that did not come from placeholders")

    values = {**variables, "creator_name": creator_name or DEFAULT_SIGNATURE}

    def fill(text: str) -> str:
        return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)

    return fill(subject), fill(body)


def template_email(decision: Decision, variables: dict[str, str], creator_name: str | None) -> tuple[str, str]:
    brand = variables["brand_name"]
    name = creator_name or DEFAULT_SIGNATURE
    if decision == Decision.ACCEPT:
        return (f"Re: Partnership with {brand}",
                f"Hi {brand} team,\n\nThank you for the offer. I'm happy to move forward at {variables['offer']}. "
                f"Please send over the contract and brief, and I'll get started.\n\nBest,\n{name}")
    if decision == Decision.DECLINE:
        return (f"Re: Partnership with {brand}",
                f"Hi {brand} team,\n\nThank you for thinking of me. After some consideration, this collaboration isn't "
                f"the right fit for my channel right now, so I'll pass this time. I wish you a successful campaign.\n\nBest,\n{name}")
    asks = [v for k, v in variables.items() if k.startswith("ask_")]
    lines = []
    if "counter_rate" in variables:
        lines.append(f"- My rate for this scope is {variables['counter_rate']}.")
    lines += [f"- {a}" for a in asks]
    changes = "\n".join(lines) or "- I'd like to discuss the terms in a bit more detail."
    return (f"Re: Partnership with {brand}",
            f"Hi {brand} team,\n\nThank you for reaching out. I'd love to work together. "
            f"Before we move forward, I'd like to propose a few changes:\n\n{changes}\n\n"
            f"Let me know if this works for you.\n\nBest,\n{name}")


def draft_email(
    deal: DealInput,
    evaluation: EvaluationResult,
    provider: LLMProvider | None,
    config: AgentConfig,
    creator_name: str | None = None,
    on_usage=None,
) -> EmailDraft:
    variables = build_variables(deal, evaluation)

    def fallback(note: str) -> EmailDraft:
        subject, body = template_email(evaluation.decision, variables, creator_name)
        return EmailDraft(subject=subject, body=body, generated_by="template", note=note)

    if provider is None:
        return fallback("LLM not available")

    asks = [f"{{{{{k}}}}}" for k in variables if k.startswith("ask_")]
    task = {"goal": GOALS[evaluation.decision], "brand_name_placeholder": "{{brand_name}}",
            "must_include_placeholders": sorted(f"{{{{{p}}}}}" for p in required_placeholders(variables)),
            "requested_changes": asks}
    messages = [
        Message(role="system", content=EMAIL_SYSTEM_PROMPT.format(allowed=_describe(variables))),
        Message(role="user", content=json.dumps(task)),
    ]
    options = ChatOptions(max_tokens=config.email.max_tokens, temperature=config.email.temperature,
                          thinking=False, json_mode=True)
    try:
        response = provider.chat(messages, None, options)
    except LLMError as e:
        return fallback(f"LLM error: {e}")
    if on_usage:
        on_usage(response.usage)
    try:
        data = json.loads(response.message.content)
        subject, body = validate_and_fill(str(data["subject"]), str(data["body"]), variables, creator_name)
    except (ValueError, KeyError, TypeError, ValidationError) as e:
        return fallback(f"LLM draft rejected: {e}")
    return EmailDraft(subject=subject, body=body, generated_by="llm")
