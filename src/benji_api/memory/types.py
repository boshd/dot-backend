from dataclasses import dataclass
from typing import Literal, Protocol


class EmbeddingProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    memory_id: str
    memory_type: Literal["fact", "episode"]
    text: str
    score: float
    relevance_score: float
    retrieval_reason: Literal["direct", "graph"] = "direct"

    def as_trace(self) -> dict[str, str | float]:
        return {
            "id": self.memory_id,
            "type": self.memory_type,
            "text": self.text,
            "score": round(self.score, 6),
            "relevance_score": round(self.relevance_score, 6),
            "retrieval_reason": self.retrieval_reason,
        }


@dataclass(frozen=True, slots=True)
class MemoryContext:
    facts: tuple[str, ...] = ()
    episodes: tuple[str, ...] = ()
    retrieved: tuple[RetrievedMemory, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.facts and not self.episodes

    def trace_snapshot(self) -> list[dict[str, str | float]]:
        return [item.as_trace() for item in self.retrieved]
