from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WebSearchSource:
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    summary: str
    sources: tuple[WebSearchSource, ...]
    queries: tuple[str, ...] = ()


class WebSearchProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def search(self, *, query: str, max_sources: int) -> WebSearchResult: ...
