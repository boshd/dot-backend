from uuid import uuid4

import pytest

from benji_api.agents.providers.openai_web_search import (
    parse_openai_web_search_response,
)
from benji_api.agents.tools import SearchWebTool
from benji_api.agents.types import ToolContext
from benji_api.agents.web_search import WebSearchResult, WebSearchSource


class FakeWebSearchProvider:
    name = "fake-search"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def search(self, *, query: str, max_sources: int) -> WebSearchResult:
        self.calls.append((query, max_sources))
        return WebSearchResult(
            summary="The sandbox allows 100 messages per day.",
            sources=(
                WebSearchSource(
                    title="Rate Limits",
                    url="https://docs.linqapp.com/guides/platform/rate-limits/",
                ),
            ),
            queries=("site:docs.linqapp.com sandbox limit",),
        )


@pytest.mark.anyio
async def test_search_web_tool_returns_grounded_provider_result() -> None:
    provider = FakeWebSearchProvider()
    tool = SearchWebTool(provider, max_sources=3)

    result = await tool.execute(
        context=ToolContext(user_id=uuid4(), conversation_id=uuid4()),
        arguments={"query": "  current Linq sandbox message limit  "},
    )

    assert provider.calls == [("current Linq sandbox message limit", 3)]
    assert result["provider"] == "fake-search"
    assert result["sources"] == [
        {
            "title": "Rate Limits",
            "url": "https://docs.linqapp.com/guides/platform/rate-limits/",
        }
    ]


def test_openai_web_search_parser_prefers_cited_sources_and_deduplicates() -> None:
    result = parse_openai_web_search_response(
        {
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "queries": ["sandbox rate limit"],
                        "sources": [{"type": "url", "url": "https://example.com/fallback"}],
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "title": "Official rate limits",
                                    "url": "https://example.com/official",
                                },
                                {
                                    "type": "url_citation",
                                    "title": "Duplicate",
                                    "url": "https://example.com/official",
                                },
                            ],
                        }
                    ],
                },
            ]
        },
        output_text="100 messages per day.",
        max_sources=2,
    )

    assert result.summary == "100 messages per day."
    assert result.queries == ("sandbox rate limit",)
    assert result.sources == (
        WebSearchSource(
            title="Official rate limits",
            url="https://example.com/official",
        ),
        WebSearchSource(
            title="example.com",
            url="https://example.com/fallback",
        ),
    )
