from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from benji_api.models.user import LanguagePreference, User


class LanguagePreferenceProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["keep", "set"]
    mode: LanguagePreference


def apply_language_preference(*, user: User, proposal: LanguagePreferenceProposal) -> bool:
    if proposal.action == "keep":
        return False

    user.preferred_language_mode = proposal.mode.value
    user.language_preference_updated_at = datetime.now(UTC)
    return True
