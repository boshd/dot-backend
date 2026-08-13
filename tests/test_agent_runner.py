from typing import Any

import pytest

from benji_api.agents.runner import AgentRunner
from benji_api.agents.tools import ToolRegistry
from benji_api.agents.types import (
    AgentMessage,
    ModelSession,
    ModelToolCall,
    ModelToolOutput,
    ModelTurn,
    StructuredOutputDefinition,
    ToolContext,
    ToolDefinition,
)


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.session = FakeModelSession()

    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        assert "lowercase" in instructions
        assert messages[-1].content == "what time is it?"
        assert tools[0].name == "echo"
        assert output is not None and output.name == "benji_conversation_turn"
        return self.session


class FakeModelSession:
    def __init__(self) -> None:
        self.turn = 0

    async def next(self, tool_outputs: tuple[ModelToolOutput, ...] = ()) -> ModelTurn:
        self.turn += 1
        if self.turn == 1:
            assert tool_outputs == ()
            return ModelTurn(
                response_id="response-1",
                text=None,
                tool_calls=(
                    ModelToolCall(call_id="call-1", name="echo", arguments={"value": "hi"}),
                ),
            )
        assert len(tool_outputs) == 1
        assert '"hi"' in tool_outputs[0].output
        return ModelTurn(
            response_id="response-2",
            text=(
                '{"messages":["it worked"],'
                '"follow_up":{"should_schedule":false,"goal":"","due_after_seconds":0},'
                '"language_preference":{"action":"set","mode":"egyptian_franco"},'
                '"reaction":{"type":"like"}}'
            ),
        )


class EchoTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="echo",
            description="Echo a value.",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )

    async def execute(self, *, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        assert context.user_id.int == 1
        return {"echo": arguments["value"]}


@pytest.mark.anyio
async def test_agent_runner_executes_tools_until_it_gets_final_text() -> None:
    from uuid import UUID

    provider = FakeProvider()
    runner = AgentRunner(provider=provider, tools=ToolRegistry([EchoTool()]))

    result = await runner.run(
        instructions="reply in lowercase",
        messages=[AgentMessage(role="user", content="what time is it?")],
        context=ToolContext(
            user_id=UUID(int=1),
            conversation_id=UUID(int=2),
        ),
    )

    assert result.text == "it worked"
    assert result.response_id == "response-2"
    assert result.tool_calls[0].name == "echo"
    assert result.tool_calls[0].succeeded is True
    assert result.language_preference is not None
    assert result.language_preference.action == "set"
    assert result.language_preference.mode.value == "egyptian_franco"
    assert result.reaction == "like"
    assert result.raw_output is not None
    assert result.raw_output["language_preference"] == {
        "action": "set",
        "mode": "egyptian_franco",
    }
