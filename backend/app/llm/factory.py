"""按配置创建模型 Provider。新增模型时在 PROVIDERS 里注册一个构造函数即可。"""

from collections.abc import Callable

from app.config import AgentConfig
from app.llm.base import LLMError, LLMProvider
from app.llm.deepseek import DeepSeekProvider
from app.settings import Settings


def _deepseek(settings: Settings, config: AgentConfig) -> LLMProvider:
    if not settings.has_deepseek_key:
        raise LLMError("DEEPSEEK_API_KEY is not set in .env", status=401)
    return DeepSeekProvider(
        settings.deepseek_api_key,
        settings.llm_model or config.model.name,
        timeout=config.model.request_timeout_seconds,
        max_retries=config.model.max_retries,
    )


PROVIDERS: dict[str, Callable[[Settings, AgentConfig], LLMProvider]] = {
    "deepseek": _deepseek,
}


def create_llm_provider(settings: Settings, config: AgentConfig) -> LLMProvider:
    try:
        factory = PROVIDERS[settings.llm_provider]
    except KeyError:
        raise LLMError(f"Unknown LLM_PROVIDER: {settings.llm_provider}") from None
    return factory(settings, config)
