import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from benji_api.agents.conversation_output import LANGUAGE_PREFERENCE_OUTPUT_SCHEMA
from benji_api.agents.types import StructuredOutputDefinition
from benji_api.models.user import OnboardingStatus, OnboardingStep, User
from benji_api.services.language_preferences import LanguagePreferenceProposal

OPT_OUT_KEYWORDS = {"stop", "unsubscribe", "optout", "opt out", "cancel", "end", "quit"}
OPT_IN_KEYWORDS = {"start", "unstop", "subscribe"}

ONBOARDING_OUTPUT = StructuredOutputDefinition(
    name="onboarding_turn",
    description="A natural onboarding reply and profile facts grounded in the conversation.",
    schema={
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "items": {
                    "type": "string",
                    "description": "One complete, natural text bubble to send as-is.",
                },
                "minItems": 1,
            },
            "profile": {
                "type": "object",
                "properties": {
                    "display_name": {"type": ["string", "null"]},
                    "birth_date": {"type": ["string", "null"]},
                    "location_city": {"type": ["string", "null"]},
                    "location_country": {"type": ["string", "null"]},
                },
                "required": [
                    "display_name",
                    "birth_date",
                    "location_city",
                    "location_country",
                ],
                "additionalProperties": False,
            },
            "language_preference": LANGUAGE_PREFERENCE_OUTPUT_SCHEMA,
        },
        "required": ["messages", "profile", "language_preference"],
        "additionalProperties": False,
    },
)


class OnboardingProfileCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None
    birth_date: str | None
    location_city: str | None
    location_country: str | None


class OnboardingTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[str] = Field(min_length=1)
    profile: OnboardingProfileCandidates
    language_preference: LanguagePreferenceProposal | None = None


@dataclass(frozen=True, slots=True)
class MessagingPreferenceResult:
    opted_out: bool
    opted_in: bool = False


@dataclass(frozen=True, slots=True)
class ProfileUpdateResult:
    completed: bool
    rejected_fields: tuple[str, ...]


def apply_messaging_preference(*, user: User, text: str) -> MessagingPreferenceResult:
    normalized = _normalized_intent(text)
    if _is_opt_out(normalized):
        user.messaging_opted_out_at = datetime.now(UTC)
        return MessagingPreferenceResult(opted_out=True)

    if user.messaging_opted_out_at is None:
        return MessagingPreferenceResult(opted_out=False)

    if normalized not in OPT_IN_KEYWORDS:
        return MessagingPreferenceResult(opted_out=True)

    user.messaging_opted_out_at = None
    return MessagingPreferenceResult(opted_out=False, opted_in=True)


def apply_profile_candidates(
    *,
    user: User,
    candidates: OnboardingProfileCandidates,
) -> ProfileUpdateResult:
    rejected: list[str] = []

    if candidates.display_name is not None:
        display_name = _validate_display_name(candidates.display_name)
        if display_name is None:
            rejected.append("display_name")
        else:
            user.display_name = display_name

    if candidates.birth_date is not None:
        birth_date = _validate_birth_date(candidates.birth_date)
        if birth_date is None:
            rejected.append("birth_date")
        else:
            user.birth_date = birth_date

    if candidates.location_city is not None:
        location_city = _validate_location(candidates.location_city)
        if location_city is None:
            rejected.append("location_city")
        else:
            user.location_city = location_city

    if candidates.location_country is not None:
        location_country = _validate_location(candidates.location_country)
        if location_country is None:
            rejected.append("location_country")
        else:
            user.location_country = location_country

    _sync_location_text(user)
    completed = _sync_onboarding_state(user)
    return ProfileUpdateResult(completed=completed, rejected_fields=tuple(rejected))


def missing_profile_fields(user: User) -> tuple[str, ...]:
    missing: list[str] = []
    if not user.display_name:
        missing.append("preferred name")
    if user.birth_date is None:
        missing.append("full date of birth (day, month, and year)")
    if not user.location_country:
        missing.append("location country")
    return tuple(missing)


def validation_repair_reply(rejected_fields: tuple[str, ...]) -> str | None:
    if "birth_date" in rejected_fields:
        return "i need the full date—day, month, and year. what’s your date of birth?"
    if "location_country" in rejected_fields:
        return "i’m not confident i got the country right—what country are you based in?"
    if "display_name" in rejected_fields:
        return "i didn’t quite catch the name you want me to use—what should i call you?"
    return None


def _sync_onboarding_state(user: User) -> bool:
    missing = missing_profile_fields(user)
    if not missing:
        user.onboarding_step = OnboardingStep.COMPLETE.value
        user.onboarding_status = OnboardingStatus.COMPLETE.value
        if user.profile_completed_at is None:
            user.profile_completed_at = datetime.now(UTC)
        return True

    user.onboarding_status = OnboardingStatus.COLLECTING_PROFILE.value
    user.profile_completed_at = None
    if not user.display_name:
        user.onboarding_step = OnboardingStep.NAME.value
    elif user.birth_date is None:
        user.onboarding_step = OnboardingStep.BIRTH_DATE.value
    else:
        user.onboarding_step = OnboardingStep.LOCATION.value
    return False


def _sync_location_text(user: User) -> None:
    if user.location_city and user.location_country:
        user.location_text = f"{user.location_city}, {user.location_country}"
    elif user.location_country:
        user.location_text = user.location_country
    elif user.location_city:
        user.location_text = user.location_city


def _validate_display_name(value: str) -> str | None:
    normalized = " ".join(value.strip().split()).strip(" .")
    if not normalized or len(normalized) > 120:
        return None
    if _normalized_intent(normalized) in {"hi", "hello", "hey", "yo", "how are you"}:
        return None
    if not re.fullmatch(r"[\w' -]+", normalized, flags=re.UNICODE):
        return None
    return normalized


def _validate_birth_date(value: str) -> date | None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    today = date.today()
    if parsed >= today or parsed.year < today.year - 120:
        return None
    return parsed


def _validate_location(value: str) -> str | None:
    normalized = " ".join(value.strip().split()).strip(" .")
    if not normalized or len(normalized) > 120:
        return None
    return normalized


def _normalized_intent(text: str) -> str:
    return " ".join(re.sub(r"[^a-zA-Z' ]", " ", text).lower().split())


def _is_opt_out(text: str) -> bool:
    if text in OPT_OUT_KEYWORDS:
        return True
    return bool(
        re.search(
            r"\b(?:stop|quit|end|cancel)\b.*\b(?:texting|messages?|contacting)\b|"
            r"\b(?:do not|don't)\s+(?:text|message|contact)\s+me\b|\bremove me\b",
            text,
        )
    )


def parse_onboarding_turn(data: dict[str, Any]) -> OnboardingTurn:
    return OnboardingTurn.model_validate(data)
