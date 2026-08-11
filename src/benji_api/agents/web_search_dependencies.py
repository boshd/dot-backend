import logging

from benji_api.agents.providers.openai_web_search import OpenAIWebSearchProvider
from benji_api.agents.web_search import WebSearchProvider
from benji_api.config import Settings

logger = logging.getLogger(__name__)


def build_web_search_provider(settings: Settings) -> WebSearchProvider | None:
    if not settings.web_search_enabled or settings.openai_api_key is None:
        return None
    if settings.web_search_provider != "openai":
        logger.error("Unsupported web search provider: %s", settings.web_search_provider)
        return None
    return OpenAIWebSearchProvider(
        api_key=settings.openai_api_key,
        model=settings.web_search_model,
        reasoning_effort=settings.web_search_reasoning_effort,
    )
