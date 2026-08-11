from openai import AsyncOpenAI

from benji_api.config import Settings
from benji_api.memory.types import EmbeddingProvider
from benji_api.models.memory import MEMORY_EMBEDDING_DIMENSIONS


class OpenAIEmbeddingProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int,
        timeout_seconds: float = 20,
    ) -> None:
        if dimensions != MEMORY_EMBEDDING_DIMENSIONS:
            raise ValueError(
                "MEMORY_EMBEDDING_DIMENSIONS must match the database vector dimension "
                f"({MEMORY_EMBEDDING_DIMENSIONS})"
            )
        self.model = model
        self.dimensions = dimensions
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.embeddings.create(
            model=self.model,
            input=texts,
            dimensions=self.dimensions,
            encoding_format="float",
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [list(item.embedding) for item in ordered]
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding provider returned an unexpected result count")
        return vectors


def build_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    if not settings.memory_enabled or settings.openai_api_key is None:
        return None
    if settings.memory_embedding_provider != "openai":
        return None
    return OpenAIEmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.memory_embedding_model,
        dimensions=settings.memory_embedding_dimensions,
    )
