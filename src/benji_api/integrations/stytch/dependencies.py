from typing import Annotated

from fastapi import Depends

from benji_api.config import Settings, get_settings
from benji_api.integrations.stytch.client import StytchAuthClient


def get_stytch_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> StytchAuthClient | None:
    if settings.stytch_project_id is None or settings.stytch_secret is None:
        return None
    return StytchAuthClient(
        project_id=settings.stytch_project_id,
        secret=settings.stytch_secret,
    )
