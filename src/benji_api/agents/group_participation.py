from dataclasses import dataclass

from benji_api.agents.prompts.base import PromptModule
from benji_api.agents.text_style import plain_text_bubble
from benji_api.agents.types import (
    AgentMessage,
    ModelProvider,
    StructuredOutputDefinition,
)

GROUP_PARTICIPATION_OUTPUT = StructuredOutputDefinition(
    name="dot_group_participation",
    description="Decide whether Dot naturally participates in the latest group-chat moment.",
    schema={
        "type": "object",
        "properties": {
            "should_respond": {"type": "boolean"},
            "send_acknowledgment": {"type": "boolean"},
            "acknowledgment": {"type": "string"},
        },
        "required": [
            "should_respond",
            "send_acknowledgment",
            "acknowledgment",
        ],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True, slots=True)
class GroupParticipationDecision:
    should_respond: bool
    acknowledgment: str | None = None


async def decide_group_participation(
    *,
    provider: ModelProvider,
    messages: list[AgentMessage],
    group_module: PromptModule,
    force_response: bool,
) -> GroupParticipationDecision:
    forced = (
        "the latest message explicitly invoked or replied to dot, so should_respond must be true."
        if force_response
        else "decide naturally whether dot should participate."
    )
    result = await provider.generate_structured(
        instructions=f"""
you are deciding whether dot should speak in a group chat, before dot writes the real response.

{group_module.content}

{forced}

use the ordinary-friend threshold. respond when the latest message is directed at dot, naturally
continues dot's question or work, asks the group for information or an opinion, advances shared
planning, calls back to the live thread, corrects dot, or gives dot room for a quick useful or funny
reaction. do not require the name "dot" and do not demand that every contribution be materially
helpful. stay silent for clearly private side chatter, pure noise, repeated details already covered,
or a reaction where another dot message would crowd the humans.

send_acknowledgment should be true only when the real response will visibly take time because it
needs a tool, web lookup, app creation, or several reasoning steps. the acknowledgment must be one
short, generative, lowercase text in the group's tone. it should react to the actual moment instead
of using a stock phrase. it must not use markdown, promise a result, repeat the request, overuse an
em dash, or ask a question.
for an ordinary conversational response, set send_acknowledgment false and acknowledgment to "".
""".strip(),
        messages=messages,
        output=GROUP_PARTICIPATION_OUTPUT,
    )
    should_respond = force_response or result.data.get("should_respond") is True
    should_ack = should_respond and result.data.get("send_acknowledgment") is True
    raw_ack = result.data.get("acknowledgment")
    acknowledgment = (
        plain_text_bubble(raw_ack)[:180]
        if should_ack and isinstance(raw_ack, str) and raw_ack.strip()
        else None
    )
    return GroupParticipationDecision(
        should_respond=should_respond,
        acknowledgment=acknowledgment,
    )
