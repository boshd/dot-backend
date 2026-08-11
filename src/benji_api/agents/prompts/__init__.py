from collections.abc import Iterable

from benji_api.agents.prompts.base import (
    BENJI_CORE_PROMPT,
    DIRECT_CONVERSATION_PROMPT,
    PromptModule,
    compose_prompt,
)
from benji_api.agents.prompts.language import build_language_module
from benji_api.agents.prompts.profile import build_user_profile_module
from benji_api.models.user import User


def build_benji_instructions(
    user: User,
    *,
    state_modules: Iterable[PromptModule] = (),
    include_private_profile: bool = True,
) -> str:
    modules = [BENJI_CORE_PROMPT]
    if include_private_profile:
        modules.append(DIRECT_CONVERSATION_PROMPT)
        modules.append(build_language_module(user))
        modules.append(build_user_profile_module(user))
    else:
        modules.append(build_language_module(None))
    modules.extend(state_modules)
    return compose_prompt(*modules)


__all__ = ["PromptModule", "build_benji_instructions", "build_language_module"]
