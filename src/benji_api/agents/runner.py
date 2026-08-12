import json
from dataclasses import dataclass, replace
from typing import Any

from benji_api.agents.conversation_output import (
    CONVERSATION_OUTPUT,
    FollowUpProposal,
    parse_conversation_output,
)
from benji_api.agents.tools import ToolRegistry
from benji_api.agents.types import (
    AgentMessage,
    ModelProvider,
    ModelToolOutput,
    ToolContext,
)
from benji_api.services.language_preferences import LanguagePreferenceProposal


class AgentRunError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutedToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    output: dict[str, Any]
    succeeded: bool


@dataclass(frozen=True, slots=True)
class AgentResult:
    messages: tuple[str, ...]
    response_id: str
    tool_calls: tuple[ExecutedToolCall, ...]
    follow_up: FollowUpProposal | None = None
    language_preference: LanguagePreferenceProposal | None = None
    raw_output: dict[str, Any] | None = None
    token_usage: dict[str, int] | None = None

    @property
    def text(self) -> str:
        return self.messages[0]


class AgentRunner:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: ToolRegistry,
        max_tool_rounds: int = 5,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._max_tool_rounds = max_tool_rounds

    async def run(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        context: ToolContext,
    ) -> AgentResult:
        model_session = self._provider.start(
            instructions=instructions,
            messages=messages,
            tools=self._tools.definitions(),
            output=CONVERSATION_OUTPUT,
        )
        outputs: tuple[ModelToolOutput, ...] = ()
        executions: list[ExecutedToolCall] = []
        token_usage: dict[str, int] = {}

        for _ in range(self._max_tool_rounds + 1):
            turn = await model_session.next(outputs)
            _merge_token_usage(token_usage, turn.token_usage)
            if not turn.tool_calls:
                if turn.text is None:
                    raise AgentRunError("Model returned neither text nor tool calls")
                conversation_output = parse_conversation_output(turn.text)
                return AgentResult(
                    messages=conversation_output.messages,
                    response_id=turn.response_id,
                    tool_calls=tuple(executions),
                    follow_up=conversation_output.follow_up,
                    language_preference=conversation_output.language_preference,
                    raw_output=_raw_output(turn.text),
                    token_usage=token_usage or None,
                )

            next_outputs = []
            for call in turn.tool_calls:
                output, succeeded = await self._tools.execute(
                    name=call.name,
                    context=replace(context, tool_call_id=call.call_id),
                    arguments=call.arguments,
                )
                executions.append(
                    ExecutedToolCall(
                        call_id=call.call_id,
                        name=call.name,
                        arguments=call.arguments,
                        output=output,
                        succeeded=succeeded,
                    )
                )
                next_outputs.append(
                    ModelToolOutput(call_id=call.call_id, output=json.dumps(output))
                )
            outputs = tuple(next_outputs)

        raise AgentRunError("Model exceeded the maximum tool-call rounds")


def _raw_output(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _merge_token_usage(total: dict[str, int], usage: dict[str, int] | None) -> None:
    if usage is None:
        return
    for key, value in usage.items():
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
