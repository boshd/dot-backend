import logging
from typing import Annotated

from fastapi import Depends

from benji_api.agents.providers.openai import OpenAIModelProvider
from benji_api.agents.tools import ToolRegistry, build_default_tool_registry
from benji_api.agents.types import ModelProvider
from benji_api.config import Settings, get_settings

logger = logging.getLogger(__name__)


def get_model_provider(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ModelProvider | None:
    return build_model_provider(settings)


def build_model_provider(settings: Settings) -> ModelProvider | None:
    if settings.openai_api_key is None:
        return None
    if settings.agent_model_provider != "openai":
        logger.error("Unsupported model provider: %s", settings.agent_model_provider)
        return None
    return OpenAIModelProvider(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        reasoning_effort=settings.openai_reasoning_effort,
    )


def get_tool_registry(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ToolRegistry:
    return build_default_tool_registry(settings)
