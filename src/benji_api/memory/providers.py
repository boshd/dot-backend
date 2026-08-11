from benji_api.agents.providers.openai import OpenAIModelProvider
from benji_api.agents.types import ModelProvider
from benji_api.config import Settings


def build_memory_model_provider(settings: Settings) -> ModelProvider | None:
    if not settings.memory_enabled or settings.openai_api_key is None:
        return None
    if settings.memory_model_provider != "openai":
        return None
    return OpenAIModelProvider(
        api_key=settings.openai_api_key,
        model=settings.memory_model,
        reasoning_effort=settings.memory_reasoning_effort,
    )
