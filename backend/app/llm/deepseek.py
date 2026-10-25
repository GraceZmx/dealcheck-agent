"""DeepSeek 适配器：把统一格式翻译成 DeepSeek（OpenAI 兼容）的 Chat Completions 接口。

直接用 httpx，不依赖厂商 SDK。重试策略按官方建议：408 / 429 / 5xx / 网络超时等待后重试，其他 4xx 不重试。
"""

import time
from collections.abc import Callable

import httpx

from app.llm.base import ChatOptions, LLMError, LLMProvider, LLMResponse, Message, ToolCall, ToolSpec, Usage

BASE_URL = "https://api.deepseek.com"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def to_api_message(m: Message) -> dict:
    msg: dict = {"role": m.role, "content": m.content}
    if m.role == "assistant":
        if m.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in m.tool_calls
            ]
        if m.reasoning_content:
            msg["reasoning_content"] = m.reasoning_content
    if m.role == "tool":
        msg["tool_call_id"] = m.tool_call_id
    return msg


def to_api_tool(t: ToolSpec) -> dict:
    return {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}


def build_request_body(model: str, messages: list[Message], tools: list[ToolSpec] | None, options: ChatOptions) -> dict:
    body: dict = {
        "model": model,
        "messages": [to_api_message(m) for m in messages],
        "max_tokens": options.max_tokens,
        "thinking": {"type": "enabled" if options.thinking else "disabled"},
    }
    if options.thinking:
        body["reasoning_effort"] = options.reasoning_effort
    else:
        body["temperature"] = options.temperature  # 思考模式下不传 temperature
    if tools:
        body["tools"] = [to_api_tool(t) for t in tools]
    if options.json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def parse_response(data: dict, latency: float) -> LLMResponse:
    choice = data["choices"][0]
    m = choice["message"]
    usage = data.get("usage") or {}
    return LLMResponse(
        message=Message(
            role="assistant",
            content=m.get("content") or "",
            reasoning_content=m.get("reasoning_content") or None,
            tool_calls=[
                ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=tc["function"].get("arguments") or "{}")
                for tc in m.get("tool_calls") or []
            ],
        ),
        finish_reason=choice.get("finish_reason") or "",
        usage=Usage(
            input_tokens=usage.get("prompt_tokens", 0),
            cached_input_tokens=usage.get("prompt_cache_hit_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0,
        ),
        latency_seconds=round(latency, 3),
        model=data.get("model", ""),
    )


def _error_message(response: httpx.Response) -> str:
    try:
        return response.json()["error"]["message"]
    except Exception:
        return response.text[:200]


class DeepSeekProvider(LLMProvider):
    name = "deepseek"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 60,
        max_retries: int = 2,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise LLMError("DeepSeek API key is missing", status=401)
        self.model = model
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._url = f"{base_url}/chat/completions"
        self._client = client or httpx.Client(timeout=timeout)
        self._max_retries = max_retries
        self._sleep = sleep

    def chat(self, messages, tools=None, options=None) -> LLMResponse:
        options = options or ChatOptions()
        body = build_request_body(self.model, messages, tools, options)

        last_error: LLMError | None = None
        for attempt in range(self._max_retries + 1):
            if attempt:
                self._sleep(2 ** (attempt - 1))
            start = time.monotonic()
            try:
                response = self._client.post(self._url, headers=self._headers, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = LLMError(f"DeepSeek request failed: {type(e).__name__}", retryable=True)
                continue
            if response.status_code == 200:
                return parse_response(response.json(), time.monotonic() - start)
            retryable = response.status_code in RETRYABLE_STATUS
            last_error = LLMError(
                f"DeepSeek HTTP {response.status_code}: {_error_message(response)}",
                status=response.status_code,
                retryable=retryable,
            )
            if not retryable:
                break
        raise last_error
