from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AgentAttachment:
    kind: Literal["image", "file", "media"]
    mime_type: str | None
    filename: str | None
    url: str | None
    provider: str
    provider_id: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class AgentMessage:
    role: Literal["user", "assistant"]
    content: str
    attachments: tuple[AgentAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelToolOutput:
    call_id: str
    output: str


@dataclass(frozen=True, slots=True)
class ModelTurn:
    response_id: str
    text: str | None
    tool_calls: tuple[ModelToolCall, ...] = ()
    token_usage: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class StructuredOutputDefinition:
    name: str
    description: str
    schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StructuredModelResult:
    response_id: str
    data: dict[str, Any]
    token_usage: dict[str, int] | None = None


class ModelSession(Protocol):
    async def next(self, tool_outputs: tuple[ModelToolOutput, ...] = ()) -> ModelTurn: ...


class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def start(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        tools: list[ToolDefinition],
        output: StructuredOutputDefinition | None = None,
    ) -> ModelSession: ...

    async def generate_structured(
        self,
        *,
        instructions: str,
        messages: list[AgentMessage],
        output: StructuredOutputDefinition,
    ) -> StructuredModelResult: ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    user_id: UUID
    conversation_id: UUID
    agent_run_id: UUID | None = None
    tool_call_id: str | None = None
    delivery_provider: str | None = None


class AgentTool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    async def execute(
        self, *, context: ToolContext, arguments: dict[str, Any]
    ) -> dict[str, Any]: ...
