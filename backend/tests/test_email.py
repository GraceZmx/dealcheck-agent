"""邮件草稿测试：占位符填值、数字只能来自规则、失败时改用模板、姓名不发给模型。"""

import json

import pytest

from app.agent.email import build_variables, draft_email, template_email, validate_and_fill
from app.config import load_agent_config
from app.llm.base import LLMError, Message
from app.llm.mock import MockProvider, assistant
from app.models import Decision
from app.rules.engine import evaluate_deal
from tests.test_engine import load_example

CONFIG = load_agent_config()


def maya():
    req = load_example("2_negotiate_maya.json")
    return req.deal, evaluate_deal(req)


def json_reply(subject, body) -> Message:
    return assistant(json.dumps({"subject": subject, "body": body}))


GOOD_BODY = ("Hi {{brand_name}} team,\n\nThanks for the offer of {{offer}}. My rate for this is {{counter_rate}}. "
             "I'd also ask: {{ask_1}} and {{ask_2}}\n\nBest,\n{{creator_name}}")


# ---------- 占位符的值 ----------


def test_negotiate_variables_come_from_rules():
    deal, ev = maya()
    v = build_variables(deal, ev)
    assert v["offer"] == "$450"
    assert v["counter_rate"] == "$830"  # 调整后 P75 = 834，取整到 10
    assert v["ask_1"].startswith("Ask for a kill fee of at least 50%")
    assert v["ask_2"].startswith("Limit revisions to 2 rounds")


def test_accept_and_decline_have_no_counter_or_asks():
    req = load_example("1_accept_fitness_tiktok.json")
    assert set(build_variables(req.deal, evaluate_deal(req))) == {"brand_name", "offer"}


# ---------- 校验与填值 ----------


def test_valid_draft_is_filled():
    deal, ev = maya()
    subject, body = validate_and_fill("Re: {{brand_name}}", GOOD_BODY, build_variables(deal, ev), "Maya")
    assert subject == "Re: Glow Theory Skincare"
    assert "$830" in body and "Maya" in body and "{{" not in body


def test_missing_name_uses_placeholder_signature():
    deal, ev = maya()
    _, body = validate_and_fill("Hi", GOOD_BODY, build_variables(deal, ev), None)
    assert body.endswith("[Your name]")


@pytest.mark.parametrize(
    "body, problem",
    [
        (GOOD_BODY + " Or $900 flat.", "numbers"),
        (GOOD_BODY + " Payment within 30 days please.", "numbers"),
        (GOOD_BODY + " A 50% deposit.", "numbers"),
        (GOOD_BODY.replace(" and {{ask_2}}", ""), "missing"),
        (GOOD_BODY.replace("{{creator_name}}", "Maya"), "missing"),
        (GOOD_BODY + " {{secret_value}}", "unknown"),
    ],
)
def test_invalid_drafts_rejected(body, problem):
    deal, ev = maya()
    with pytest.raises(ValueError, match=problem):
        validate_and_fill("Hi", body, build_variables(deal, ev), None)


# ---------- draft_email ----------


def test_llm_draft_used_when_valid():
    deal, ev = maya()
    provider = MockProvider([json_reply("Re: {{brand_name}}", GOOD_BODY)])
    email = draft_email(deal, ev, provider, CONFIG, creator_name="Maya")
    assert email.generated_by == "llm" and "$830" in email.body
    opts = provider.calls[0]["options"]
    assert opts.json_mode and not opts.thinking
    assert "Maya" not in json.dumps([m.content for m in provider.calls[0]["messages"]])  # 姓名不发给模型
    assert "$830" not in json.dumps([m.content for m in provider.calls[0]["messages"]])  # 金额也不发给模型


def test_invalid_llm_draft_falls_back_to_template():
    deal, ev = maya()
    email = draft_email(deal, ev, MockProvider([json_reply("Hi", "I want $2,000.")]), CONFIG)
    assert email.generated_by == "template" and "rejected" in email.note
    assert "$830" in email.body and "kill fee" in email.body


def test_non_json_reply_falls_back():
    deal, ev = maya()
    assert draft_email(deal, ev, MockProvider([assistant("not json")]), CONFIG).generated_by == "template"


def test_llm_error_falls_back():
    deal, ev = maya()
    email = draft_email(deal, ev, MockProvider([LLMError("HTTP 500", status=500)]), CONFIG)
    assert email.generated_by == "template" and "LLM error" in email.note


def test_no_provider_uses_template():
    deal, ev = maya()
    assert draft_email(deal, ev, None, CONFIG).generated_by == "template"


def test_usage_callback_called():
    deal, ev = maya()
    seen = []
    draft_email(deal, ev, MockProvider([json_reply("Hi", GOOD_BODY)]), CONFIG, on_usage=seen.append)
    assert len(seen) == 1


@pytest.mark.parametrize("decision", list(Decision))
def test_templates_contain_no_unfilled_placeholders(decision):
    deal, ev = maya()
    subject, body = template_email(decision, build_variables(deal, ev), "Maya")
    assert "{{" not in subject + body and "Maya" in body


def test_decline_template_does_not_mention_concerns():
    req = load_example("4_decline_upfront_payment.json")
    _, body = template_email(Decision.DECLINE, build_variables(req.deal, evaluate_deal(req)), None)
    assert "scam" not in body.lower() and "upfront" not in body.lower()
