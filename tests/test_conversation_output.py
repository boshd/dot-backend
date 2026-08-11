import json

from benji_api.agents.conversation_output import CONVERSATION_OUTPUT, parse_conversation_output
from benji_api.agents.prompts.base import BENJI_CORE_PROMPT, DOT_PROMPT_VERSION
from benji_api.models.user import LanguagePreference


def test_conversation_output_does_not_impose_a_model_visible_bubble_quota() -> None:
    message_schema = CONVERSATION_OUTPUT.schema["properties"]["messages"]

    assert "maxItems" not in message_schema
    assert "natural text bubble" in message_schema["items"]["description"].lower()
    assert "language_preference" in CONVERSATION_OUTPUT.schema["required"]


def test_conversation_output_preserves_the_models_natural_segmentation() -> None:
    messages = [f"beat {index}" for index in range(6)]

    turn = parse_conversation_output(
        json.dumps(
            {
                "messages": messages,
                "follow_up": {
                    "should_schedule": False,
                    "goal": "",
                    "due_after_seconds": 0,
                },
            }
        )
    )

    assert turn.messages == tuple(messages)
    assert turn.language_preference is None


def test_conversation_output_parses_private_language_preference_proposal() -> None:
    turn = parse_conversation_output(
        json.dumps(
            {
                "messages": ["3amel eh ya basha"],
                "follow_up": {
                    "should_schedule": False,
                    "goal": "",
                    "due_after_seconds": 0,
                },
                "language_preference": {
                    "action": "set",
                    "mode": "egyptian_franco",
                },
            }
        )
    )

    assert turn.language_preference is not None
    assert turn.language_preference.action == "set"
    assert turn.language_preference.mode is LanguagePreference.EGYPTIAN_FRANCO


def test_core_prompt_optimizes_for_conversational_momentum_not_a_bubble_count() -> None:
    prompt = BENJI_CORE_PROMPT.content
    normalized = " ".join(prompt.split())

    assert "1–4" not in prompt
    assert "choose the breaks by feel" in normalized
    assert "direct questions deserve direct answers" in normalized
    assert "don't tack a question onto every response" in normalized
    assert 'treat short replies like "yeah", "sure", "do it", or "why?"' in normalized
    assert "each substantive turn should advance it" in normalized
    assert "closes only the immediate social beat" in normalized
    assert 'avoid vague handoffs such as "what now?", "how can i help?"' in normalized


def test_core_prompt_avoids_copy_ready_positioning_and_teaches_behavioral_contrasts() -> None:
    prompt = BENJI_CORE_PROMPT.content
    normalized = " ".join(prompt.split()).lower()

    assert "personal ai companion" not in normalized
    assert "capable friend" not in normalized
    assert "you live in their texts" not in normalized
    assert "these are contrasts between weak and strong behavior, not lines to copy" in normalized

    for scenario in (
        "identity question",
        "greeting after shared work",
        "short continuation",
        "pushback or annoyance",
        "text bubbles",
    ):
        assert scenario in normalized


def test_core_prompt_keeps_hard_capability_privacy_and_action_policies() -> None:
    normalized = " ".join(BENJI_CORE_PROMPT.content.split()).lower()

    assert "never claim a tool action succeeded until its result confirms success" in normalized
    assert "ask for confirmation before writes, sends, purchases" in normalized
    assert "protect private information" in normalized
    assert "never invent an app link" in normalized
    assert "never silently create recurring outreach" in normalized
    assert DOT_PROMPT_VERSION
