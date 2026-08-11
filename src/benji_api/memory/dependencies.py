from typing import Annotated

from fastapi import Depends

from benji_api.config import Settings, get_settings
from benji_api.memory.embeddings import build_embedding_provider
from benji_api.memory.types import EmbeddingProvider


def get_embedding_provider(
    settings: Annotated[Settings, Depends(get_settings)],
) -> EmbeddingProvider | None:
    return build_embedding_provider(settings)
