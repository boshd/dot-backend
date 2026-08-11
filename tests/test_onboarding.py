from datetime import date

from benji_api.agents.prompts import build_benji_instructions
from benji_api.agents.prompts.onboarding import build_onboarding_module
from benji_api.models.user import LanguagePreference, OnboardingStatus, OnboardingStep, User
from benji_api.services.onboarding import (
    ONBOARDING_OUTPUT,
    OnboardingProfileCandidates,
    apply_messaging_preference,
    apply_profile_candidates,
    missing_profile_fields,
    parse_onboarding_turn,
)


def test_profile_candidates_can_complete_onboarding_in_any_order() -> None:
    user = User(
        phone_number="+14155552671",
        onboarding_status=OnboardingStatus.COLLECTING_PROFILE.value,
        onboarding_step=OnboardingStep.NAME.value,
    )

    result = apply_profile_candidates(
        user=user,
        candidates=OnboardingProfileCandidates(
            display_name="Kareem",
            birth_date="1992-04-18",
            location_city="Cairo",
            location_country="Egypt",
        ),
    )

    assert result.completed is True
    assert result.rejected_fields == ()
    assert user.display_name == "Kareem"
    assert user.birth_date == date(1992, 4, 18)
    assert user.location_city == "Cairo"
    assert user.location_country == "Egypt"
    assert user.location_text == "Cairo, Egypt"
    assert user.onboarding_step == OnboardingStep.COMPLETE.value
    assert user.onboarding_status == OnboardingStatus.COMPLETE.value


def test_partial_or_ambiguous_details_do_not_complete_profile() -> None:
    user = User(
        phone_number="+14155552671",
        display_name="Kareem",
        onboarding_status=OnboardingStatus.COLLECTING_PROFILE.value,
        onboarding_step=OnboardingStep.BIRTH_DATE.value,
    )

    result = apply_profile_candidates(
        user=user,
        candidates=OnboardingProfileCandidates(
            display_name=None,
            birth_date=None,
            location_city="Springfield",
            location_country=None,
        ),
    )

    assert result.completed is False
    assert user.birth_date is None
    assert user.location_city == "Springfield"
    assert user.location_country is None
    assert missing_profile_fields(user) == (
        "full date of birth (day, month, and year)",
        "location country",
    )


def test_invalid_structured_birth_date_is_rejected_by_application_guardrail() -> None:
    user = User(phone_number="+14155552671")

    result = apply_profile_candidates(
        user=user,
        candidates=OnboardingProfileCandidates(
            display_name="Kareem",
            birth_date="1992-02-31",
            location_city="Cairo",
            location_country="Egypt",
        ),
    )

    assert result.completed is False
    assert result.rejected_fields == ("birth_date",)
    assert user.birth_date is None


def test_onboarding_prompt_is_a_composable_conversation_state() -> None:
    user = User(phone_number="+14155552671")

    prompt = build_benji_instructions(
        user,
        state_modules=(build_onboarding_module(user, is_new_user=True),),
    )
    normalized = " ".join(prompt.split())

    assert '<prompt_module name="benji_core">' in prompt
    assert '<prompt_module name="user_profile">' in prompt
    assert '<prompt_module name="onboarding">' in prompt
    assert "no capability tools during onboarding" in prompt
    assert "save you to their contacts" in prompt
    assert "onboarding can unfold across the real conversation" in prompt
    assert "normalize it without asking for redundant confirmation" in normalized
    assert "lead with purpose" in prompt
    assert "don't mechanically follow one profile answer" in prompt
    assert "calculate their age" in prompt
    assert "it is not identity verification" in prompt
    assert "make a natural handoff in the same" in normalized
    assert 'don\'t ask a generic "how can i help?"' in normalized
    assert 'deliver an "all set" welcome speech' in normalized
    assert "do not immediately replace the last profile" in normalized


def test_onboarding_completion_guidance_prioritizes_the_live_goal() -> None:
    user = User(
        phone_number="+14155552671",
        display_name="Kareem",
        location_city="Cairo",
        location_country="Egypt",
        onboarding_status=OnboardingStatus.COLLECTING_PROFILE.value,
        onboarding_step=OnboardingStep.BIRTH_DATE.value,
    )

    module = build_onboarding_module(user, is_new_user=False)
    normalized = " ".join(module.content.split())

    assert "full date of birth (day, month, and year)" in module.content
    assert "preferred name" not in module.content.split("rules:", maxsplit=1)[0]
    assert "location country" not in module.content.split("rules:", maxsplit=1)[0]
    assert "continue an existing goal or request" in normalized
    assert "give them room" in normalized


def test_onboarding_output_preserves_natural_message_segmentation() -> None:
    messages = [f"text {index}" for index in range(6)]

    assert "maxItems" not in ONBOARDING_OUTPUT.schema["properties"]["messages"]
    assert "language_preference" in ONBOARDING_OUTPUT.schema["required"]
    turn = parse_onboarding_turn(
        {
            "messages": messages,
            "profile": {
                "display_name": None,
                "birth_date": None,
                "location_city": None,
                "location_country": None,
            },
        }
    )

    assert turn.messages == messages
    assert turn.language_preference is None


def test_onboarding_output_parses_private_language_preference_proposal() -> None:
    turn = parse_onboarding_turn(
        {
            "messages": ["tab esmak eh?"],
            "profile": {
                "display_name": None,
                "birth_date": None,
                "location_city": None,
                "location_country": None,
            },
            "language_preference": {
                "action": "set",
                "mode": "egyptian_franco",
            },
        }
    )

    assert turn.language_preference is not None
    assert turn.language_preference.action == "set"
    assert turn.language_preference.mode is LanguagePreference.EGYPTIAN_FRANCO


def test_stop_and_start_remain_deterministic_compliance_guardrails() -> None:
    user = User(phone_number="+14155552671")

    stopped = apply_messaging_preference(user=user, text="please stop texting me")
    restarted = apply_messaging_preference(user=user, text="START")

    assert stopped.opted_out is True
    assert restarted.opted_out is False
    assert restarted.opted_in is True
    assert user.messaging_opted_out_at is None
