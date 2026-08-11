from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from benji_api.models.user import LanguagePreference, User
from benji_api.services.language_preferences import (
    LanguagePreferenceProposal,
    apply_language_preference,
)


def test_language_preference_proposal_accepts_only_known_actions_and_modes() -> None:
    proposal = LanguagePreferenceProposal.model_validate(
        {"action": "set", "mode": "egyptian_franco"}
    )

    assert proposal.action == "set"
    assert proposal.mode is LanguagePreference.EGYPTIAN_FRANCO

    with pytest.raises(ValidationError):
        LanguagePreferenceProposal.model_validate({"action": "guess", "mode": "english"})
    with pytest.raises(ValidationError):
        LanguagePreferenceProposal.model_validate({"action": "set", "mode": "spanish"})
    with pytest.raises(ValidationError):
        LanguagePreferenceProposal.model_validate(
            {"action": "set", "mode": "english", "reason": "user asked"}
        )


def test_keep_does_not_change_the_language_preference() -> None:
    updated_at = datetime(2026, 8, 9, tzinfo=UTC)
    user = User(
        phone_number="+14155552671",
        preferred_language_mode=LanguagePreference.ENGLISH.value,
        language_preference_updated_at=updated_at,
    )

    changed = apply_language_preference(
        user=user,
        proposal=LanguagePreferenceProposal(action="keep", mode=LanguagePreference.EGYPTIAN_FRANCO),
    )

    assert changed is False
    assert user.preferred_language_mode == LanguagePreference.ENGLISH.value
    assert user.language_preference_updated_at == updated_at


def test_set_updates_the_language_preference_and_timestamp() -> None:
    user = User(phone_number="+14155552671")
    before = datetime.now(UTC)

    changed = apply_language_preference(
        user=user,
        proposal=LanguagePreferenceProposal(action="set", mode=LanguagePreference.EGYPTIAN_FRANCO),
    )

    assert changed is True
    assert user.preferred_language_mode == LanguagePreference.EGYPTIAN_FRANCO.value
    assert user.language_preference_updated_at is not None
    assert user.language_preference_updated_at >= before
