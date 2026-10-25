"""假模型：按预设剧本依次回复。用于测试，不联网、不花钱、结果稳定。"""

from collections.abc import Callable

from app.llm.base import ChatOptions, LLMError, LLMProvider, LLMResponse, Message, ToolCall, ToolSpec, Usage

ScriptStep = Message | LLMError | Callable[[list[Message], list[ToolSpec] | None], Message]


def tool_call(name: str, arguments: str, call_id: str | None = None) -> ToolCall:
    return ToolCall(id=call_id or f"call_{name}", name=name, arguments=arguments)


def assistant(content: str = "", *calls: ToolCall, reasoning: str | None = None) -> Message:
    return Message(role="assistant", content=content, tool_calls=list(calls), reasoning_content=reasoning)


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(self, script: list[ScriptStep], model: str = "mock-model", usage: Usage | None = None):
        self.model = model
        self._script = list(script)
        self._usage = usage or Usage(input_tokens=1000, output_tokens=200)
        self.calls: list[dict] = []  # 每次调用收到的内容，供测试断言

    def chat(self, messages, tools=None, options=None) -> LLMResponse:
        self.calls.append({
            "messages": [m.model_copy(deep=True) for m in messages],
            "tools": [t.name for t in tools or []],
            "options": options or ChatOptions(),
        })
        if not self._script:
            raise AssertionError("MockProvider 剧本已用完")
        step = self._script.pop(0)
        if isinstance(step, LLMError):
            raise step
        message = step(messages, tools) if callable(step) else step
        return LLMResponse(
            message=message,
            finish_reason="tool_calls" if message.tool_calls else "stop",
            usage=self._usage,
            latency_seconds=0.01,
            model=self.model,
        )
