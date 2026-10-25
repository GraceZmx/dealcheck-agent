"""模型封装层的统一接口（PRD 10.2 节）。

业务代码只依赖这里定义的格式；每家模型写一个适配器负责双向翻译。换模型 = 新增一个适配器 + 改配置。
"""

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str = Field(description="JSON 字符串，原样保留模型的输出")


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    reasoning_content: str | None = Field(None, description="思考模式的思考内容；带工具的请求需要原样传回")


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict = Field(description="JSON Schema")


class Usage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0  # input_tokens 中命中缓存的部分
    output_tokens: int = 0  # 包含思考部分
    reasoning_tokens: int = 0  # output_tokens 中的思考部分

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(**{k: getattr(self, k) + getattr(other, k) for k in Usage.model_fields})


class ChatOptions(BaseModel):
    max_tokens: int = 4000
    temperature: float = 0.2
    thinking: bool = False
    reasoning_effort: str = "low"
    json_mode: bool = False


class LLMResponse(BaseModel):
    message: Message
    finish_reason: str
    usage: Usage
    latency_seconds: float
    model: str


class LLMError(Exception):
    """模型调用失败。message 中不包含 API Key。"""

    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        options: ChatOptions | None = None,
    ) -> LLMResponse:
        """发送一轮对话，返回模型回复（文字或工具调用）和用量。"""
