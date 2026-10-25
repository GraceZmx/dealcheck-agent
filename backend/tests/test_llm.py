"""模型封装层测试：用 httpx.MockTransport 模拟网络，不发出真实请求。"""

import json

import httpx
import pytest

from app.config import load_agent_config
from app.llm.base import ChatOptions, LLMError, Message, ToolCall, ToolSpec, Usage
from app.llm.deepseek import DeepSeekProvider, build_request_body
from app.llm.factory import PROVIDERS, create_llm_provider
from app.llm.mock import MockProvider, assistant, tool_call
from app.settings import Settings, load_settings, read_env_file

FAKE_KEY = "sk-test-not-a-real-key"
TOOL = ToolSpec(name="web_search", description="Search", parameters={"type": "object", "properties": {}})


def ok_body(message: dict, usage: dict | None = None) -> dict:
    return {
        "model": "deepseek-flash",
        "choices": [{"message": {"role": "assistant", **message}, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20},
    }


def make_provider(handler, **kwargs) -> tuple[DeepSeekProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request, len(seen))

    client = httpx.Client(transport=httpx.MockTransport(record))
    provider = DeepSeekProvider(FAKE_KEY, "deepseek-flash", client=client, sleep=lambda s: None, **kwargs)
    return provider, seen


# ---------- 请求格式 ----------


def test_request_body_thinking_disabled_uses_temperature():
    body = build_request_body("m", [Message(role="user", content="hi")], None, ChatOptions(temperature=0.3))
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0.3
    assert "reasoning_effort" not in body and "tools" not in body and "response_format" not in body


def test_request_body_thinking_enabled_uses_effort():
    body = build_request_body("m", [], None, ChatOptions(thinking=True, reasoning_effort="high"))
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    assert "temperature" not in body


def test_request_body_tools_and_json_mode():
    body = build_request_body("m", [], [TOOL], ChatOptions(json_mode=True))
    assert body["tools"][0] == {"type": "function", "function": {"name": "web_search", "description": "Search",
                                                                   "parameters": {"type": "object", "properties": {}}}}
    assert body["response_format"] == {"type": "json_object"}


def test_assistant_tool_calls_and_reasoning_are_passed_back():
    history = [
        Message(role="assistant", content="", reasoning_content="thinking...",
                tool_calls=[ToolCall(id="c1", name="web_search", arguments='{"query": "x"}')]),
        Message(role="tool", tool_call_id="c1", content='{"results": []}'),
    ]
    msgs = build_request_body("m", history, [TOOL], ChatOptions())["messages"]
    assert msgs[0]["reasoning_content"] == "thinking..."
    assert msgs[0]["tool_calls"][0] == {"id": "c1", "type": "function",
                                        "function": {"name": "web_search", "arguments": '{"query": "x"}'}}
    assert msgs[1] == {"role": "tool", "content": '{"results": []}', "tool_call_id": "c1"}


def test_api_key_sent_as_bearer_header():
    provider, seen = make_provider(lambda r, n: httpx.Response(200, json=ok_body({"content": "hi"})))
    provider.chat([Message(role="user", content="hi")])
    assert seen[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert seen[0].url == "https://api.deepseek.com/chat/completions"


# ---------- 响应解析 ----------


def test_parse_tool_calls_and_usage():
    body = ok_body(
        {"content": None, "reasoning_content": "r", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "a"}'}}]},
        usage={"prompt_tokens": 300, "completion_tokens": 60, "prompt_cache_hit_tokens": 100,
               "completion_tokens_details": {"reasoning_tokens": 40}},
    )
    provider, _ = make_provider(lambda r, n: httpx.Response(200, json=body))
    resp = provider.chat([Message(role="user", content="hi")], [TOOL])
    assert resp.message.content == ""
    assert resp.message.reasoning_content == "r"
    assert resp.message.tool_calls == [ToolCall(id="c1", name="web_search", arguments='{"query": "a"}')]
    assert resp.usage == Usage(input_tokens=300, cached_input_tokens=100, output_tokens=60, reasoning_tokens=40)


def test_usage_without_reasoning_details():
    provider, _ = make_provider(lambda r, n: httpx.Response(200, json=ok_body({"content": "x"})))
    assert provider.chat([]).usage.reasoning_tokens == 0


# ---------- 错误与重试 ----------


@pytest.mark.parametrize("status", [429, 500, 503, 408])
def test_retryable_errors_are_retried(status):
    def handler(request, n):
        return httpx.Response(status, json={"error": {"message": "busy"}}) if n == 1 else httpx.Response(200, json=ok_body({"content": "ok"}))

    provider, seen = make_provider(handler)
    assert provider.chat([]).message.content == "ok"
    assert len(seen) == 2


def test_timeout_is_retried():
    def handler(request, n):
        if n == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=ok_body({"content": "ok"}))

    provider, seen = make_provider(handler)
    assert provider.chat([]).message.content == "ok"


def test_retries_exhausted_raises():
    provider, seen = make_provider(lambda r, n: httpx.Response(429, json={"error": {"message": "rate limited"}}), max_retries=2)
    with pytest.raises(LLMError) as e:
        provider.chat([])
    assert len(seen) == 3
    assert e.value.status == 429 and e.value.retryable


@pytest.mark.parametrize("status", [400, 401, 402])
def test_non_retryable_errors_fail_immediately(status):
    provider, seen = make_provider(lambda r, n: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(LLMError) as e:
        provider.chat([])
    assert len(seen) == 1
    assert e.value.status == status and not e.value.retryable


def test_error_message_never_contains_api_key():
    provider, _ = make_provider(lambda r, n: httpx.Response(401, json={"error": {"message": "Authentication Fails"}}))
    with pytest.raises(LLMError) as e:
        provider.chat([])
    assert FAKE_KEY not in str(e.value)
    assert "Authentication Fails" in str(e.value)


def test_missing_key_rejected():
    with pytest.raises(LLMError):
        DeepSeekProvider("", "deepseek-flash")


# ---------- 假模型 ----------


def test_mock_provider_follows_script_and_records_calls():
    mock = MockProvider([assistant("", tool_call("web_search", '{"query": "a"}')), assistant("done")])
    first = mock.chat([Message(role="user", content="hi")], [TOOL])
    second = mock.chat([Message(role="user", content="again")])
    assert first.message.tool_calls[0].name == "web_search" and first.finish_reason == "tool_calls"
    assert second.message.content == "done"
    assert mock.calls[0]["tools"] == ["web_search"]
    with pytest.raises(AssertionError):
        mock.chat([])


def test_mock_provider_can_raise():
    mock = MockProvider([LLMError("boom", status=500)])
    with pytest.raises(LLMError):
        mock.chat([])


# ---------- 设置与工厂 ----------


def test_read_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# comment\nLLM_PROVIDER=deepseek\nDEEPSEEK_API_KEY="abc"\n\nBROKEN LINE\n', encoding="utf-8")
    assert read_env_file(env) == {"LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "abc"}


def test_settings_env_vars_override_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("LLM_MODEL=deepseek-flash\nDEEPSEEK_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    s = load_settings(env)
    assert s.llm_model == "deepseek-v4-pro"
    assert s.has_deepseek_key


@pytest.mark.parametrize("key, expected", [("your-key-here", False), ("", False), (None, False), ("sk-x", True)])
def test_placeholder_key_is_not_a_key(key, expected):
    assert Settings(deepseek_api_key=key).has_deepseek_key is expected


def test_settings_repr_hides_key():
    assert FAKE_KEY not in repr(Settings(deepseek_api_key=FAKE_KEY))


def test_factory_creates_deepseek_with_config_model():
    provider = create_llm_provider(Settings(deepseek_api_key=FAKE_KEY), load_agent_config())
    assert isinstance(provider, DeepSeekProvider) and provider.model == "deepseek-flash"


def test_factory_model_override():
    provider = create_llm_provider(Settings(deepseek_api_key=FAKE_KEY, llm_model="deepseek-v4-pro"), load_agent_config())
    assert provider.model == "deepseek-v4-pro"


def test_factory_without_key_raises():
    with pytest.raises(LLMError):
        create_llm_provider(Settings(deepseek_api_key="your-key-here"), load_agent_config())


def test_factory_unknown_provider():
    with pytest.raises(LLMError):
        create_llm_provider(Settings(llm_provider="nope", deepseek_api_key=FAKE_KEY), load_agent_config())


def test_switching_provider_needs_only_registration_and_config(monkeypatch):
    """换模型：注册一个新的构造函数 + 改 LLM_PROVIDER，业务代码不动。"""
    monkeypatch.setitem(PROVIDERS, "mock", lambda settings, config: MockProvider([assistant("hi")]))
    provider = create_llm_provider(Settings(llm_provider="mock"), load_agent_config())
    assert provider.chat([]).message.content == "hi"


def test_agent_config_defaults():
    config = load_agent_config()
    assert config.model.name == "deepseek-flash"
    assert config.model.thinking is False
    assert (config.budget.max_searches, config.budget.max_fetches, config.budget.max_cost_cny) == (3, 3, 0.50)
