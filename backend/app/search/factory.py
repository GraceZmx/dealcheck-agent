"""按配置创建搜索 Provider。"""

from collections.abc import Callable

from app.config import AgentConfig
from app.search.base import SearchError, SearchProvider
from app.search.deepseek import DeepSeekSearchProvider
from app.settings import Settings


def _deepseek(settings: Settings, config: AgentConfig) -> SearchProvider:
    if not settings.has_deepseek_key:
        raise SearchError("DEEPSEEK_API_KEY is not set in .env", status=401)
    return DeepSeekSearchProvider(settings.deepseek_api_key, max_retries=config.model.max_retries)


PROVIDERS: dict[str, Callable[[Settings, AgentConfig], SearchProvider]] = {
    "deepseek": _deepseek,
}


def create_search_provider(settings: Settings, config: AgentConfig) -> SearchProvider:
    try:
        factory = PROVIDERS[settings.search_provider]
    except KeyError:
        raise SearchError(f"Unknown SEARCH_PROVIDER: {settings.search_provider}") from None
    return factory(settings, config)
