import json
from typing import Any

from openai import AsyncOpenAI

from benji_api.agents.types import (
    AgentMessage,
    ModelSession,
    ModelToolCall,
    ModelToolOutput,
    ModelTurn,
    StructuredModelResult,
    StructuredOutputDefinition,
    ToolDefinition,
)


class OpenAIModelProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        reasoning_effort: str = "low",
        timeout_seconds: float = 30,
    ) -> None:
        self.model = model
        self._reasoning_effort = reasoning_effort
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds)

    @property
    def reasoning_effort(self) -> str:
        return self._reasoning_effort

    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession:
        return _OpenAIModelSession(
            client=self._client,
            model=self.model,
            reasoning_effort=self._reasoning_effort,
            instructions=instructions,
            messages=messages,
            tools=tools,
            output=output,
        )

    async def generate_structured(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        output: StructuredOutputDefinition,
    ) -> StructuredModelResult:
        response = await self._client.responses.create(
            model=self.model,
            instructions=instructions,
            input=[{"role": message.role, "content": message.content} for message in messages],
            text={
                "format": {
                    "type": "json_schema",
                    "name": output.name,
                    "description": output.description,
                    "schema": output.schema,
                    "strict": True,
                }
            },
            reasoning={"effort": self._reasoning_effort},
            max_output_tokens=800,
            store=False,
        )
        try:
            data = json.loads(response.output_text)
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeError("Model returned invalid structured output") from error
        if not isinstance(data, dict):
            raise RuntimeError("Model structured output was not an object")
        return StructuredModelResult(
            response_id=response.id,
            data=data,
            token_usage=_response_token_usage(response),
        )


class _OpenAIModelSession:
    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        model: str,
        reasoning_effort: str,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None,
    ) -> None:
        self._client = client
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._instructions = instructions
        self._input: list[Any] = [
            {"role": message.role, "content": message.content} for message in messages
        ]
        self._tools = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": True,
            }
            for tool in tools
        ]
        self._text = (
            {
                "format": {
                    "type": "json_schema",
                    "name": output.name,
                    "description": output.description,
                    "schema": output.schema,
                    "strict": True,
                }
            }
            if output is not None
            else None
        )

    async def next(self, tool_outputs: tuple[ModelToolOutput, ...] = ()) -> ModelTurn:
        self._input.extend(
            {
                "type": "function_call_output",
                "call_id": output.call_id,
                "output": output.output,
            }
            for output in tool_outputs
        )
        request: dict[str, Any] = {
            "model": self._model,
            "instructions": self._instructions,
            "input": self._input,
            "tools": self._tools,
            "reasoning": {"effort": self._reasoning_effort},
            "max_output_tokens": 1_200,
            "store": False,
        }
        if self._text is not None:
            request["text"] = self._text
        response = await self._client.responses.create(**request)
        self._input.extend(response.output)

        calls = []
        for item in response.output:
            if item.type != "function_call":
                continue
            try:
                arguments = json.loads(item.arguments)
            except json.JSONDecodeError:
                arguments = {"_invalid_json": item.arguments}
            calls.append(
                ModelToolCall(
                    call_id=item.call_id,
                    name=item.name,
                    arguments=arguments,
                )
            )
        text = response.output_text.strip() or None
        return ModelTurn(
            response_id=response.id,
            text=text,
            tool_calls=tuple(calls),
            token_usage=_response_token_usage(response),
        )


def _response_token_usage(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    result: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = value
    return result or None
