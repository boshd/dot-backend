import json
from dataclasses import dataclass

from pydantic import ValidationError

from benji_api.agents.types import StructuredOutputDefinition
from benji_api.services.language_preferences import LanguagePreferenceProposal

LANGUAGE_PREFERENCE_OUTPUT_SCHEMA = {
    "type": "object",
    "description": (
        "Private application state describing whether the user's durable language preference "
        "should change. Never mention this object in the user-facing messages."
    ),
    "properties": {
        "action": {"type": "string", "enum": ["keep", "set"]},
        "mode": {
            "type": "string",
            "enum": ["auto", "english", "arabic_script", "egyptian_franco"],
        },
    },
    "required": ["action", "mode"],
    "additionalProperties": False,
}

REACTION_OUTPUT_SCHEMA = {
    "type": "object",
    "description": (
        "An optional native reaction to the user's current message. This is only honored when "
        "the current channel explicitly supports reactions. Set type to none unless a prompt "
        "module explicitly says this exact current message supports native reactions."
    ),
    "properties": {
        "type": {
            "type": "string",
            "enum": ["none", "like", "love", "laugh", "emphasize", "question"],
        }
    },
    "required": ["type"],
    "additionalProperties": False,
}

CONVERSATION_OUTPUT = StructuredOutputDefinition(
    name="benji_conversation_turn",
    description=(
        "The texts Dot wants sent now, in order, plus an optional intent for a later follow-up if "
        "the user stays silent. Each messages item is one natural text bubble."
    ),
    schema={
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "items": {
                    "type": "string",
                    "description": "One complete, natural text bubble to send as-is.",
                },
                "minItems": 0,
            },
            "follow_up": {
                "type": "object",
                "properties": {
                    "should_schedule": {"type": "boolean"},
                    "goal": {"type": "string"},
                    "due_after_seconds": {"type": "integer"},
                },
                "required": ["should_schedule", "goal", "due_after_seconds"],
                "additionalProperties": False,
            },
            "language_preference": LANGUAGE_PREFERENCE_OUTPUT_SCHEMA,
            "reaction": REACTION_OUTPUT_SCHEMA,
        },
        "required": ["messages", "follow_up", "language_preference", "reaction"],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True, slots=True)
class FollowUpProposal:
    goal: str
    due_after_seconds: int


@dataclass(frozen=True, slots=True)
class ConversationOutput:
    messages: tuple[str, ...]
    follow_up: FollowUpProposal | None = None
    language_preference: LanguagePreferenceProposal | None = None
    reaction: str | None = None


def parse_conversation_output(text: str) -> ConversationOutput:
    """Validate model output while allowing plain-text providers during migration."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        clean = text.strip()
        if not clean:
            raise ValueError("Model returned an empty assistant turn") from None
        return ConversationOutput(messages=(clean,))

    if not isinstance(data, dict):
        raise ValueError("Conversation output must be an object")
    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("Conversation output messages must be an array")
    messages = tuple(
        message.strip() for message in raw_messages if isinstance(message, str) and message.strip()
    )
    follow_up = None
    raw_follow_up = data.get("follow_up")
    if isinstance(raw_follow_up, dict) and raw_follow_up.get("should_schedule") is True:
        goal = raw_follow_up.get("goal")
        due_after_seconds = raw_follow_up.get("due_after_seconds")
        if (
            isinstance(goal, str)
            and goal.strip()
            and isinstance(due_after_seconds, int)
            and not isinstance(due_after_seconds, bool)
        ):
            follow_up = FollowUpProposal(
                goal=goal.strip()[:500],
                due_after_seconds=due_after_seconds,
            )
    return ConversationOutput(
        messages=messages,
        follow_up=follow_up,
        language_preference=_parse_language_preference(data.get("language_preference")),
        reaction=parse_reaction(data.get("reaction")),
    )


def _parse_language_preference(value: object) -> LanguagePreferenceProposal | None:
    if value is None:
        return None
    try:
        return LanguagePreferenceProposal.model_validate(value)
    except ValidationError as error:
        raise ValueError("Conversation output language preference was invalid") from error


def parse_reaction(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Conversation output reaction was invalid")
    reaction_type = value.get("type")
    if reaction_type == "none":
        return None
    if reaction_type not in {"like", "love", "laugh", "emphasize", "question"}:
        raise ValueError("Conversation output reaction was invalid")
    return reaction_type
