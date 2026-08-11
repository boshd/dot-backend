from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from benji_api.config import Settings, get_settings
from benji_api.integrations.auth import AuthTokenVerifier
from benji_api.integrations.firebase.client import FirebaseAuthClient


@lru_cache
def _firebase_client(
    project_id: str,
    service_account_json: str | None,
    check_revoked: bool,
) -> FirebaseAuthClient:
    return FirebaseAuthClient(
        project_id=project_id,
        service_account_json=service_account_json,
        check_revoked=check_revoked,
    )


def get_auth_token_verifier(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthTokenVerifier | None:
    if settings.firebase_project_id is None:
        return None
    return _firebase_client(
        settings.firebase_project_id,
        settings.firebase_service_account_json,
        settings.firebase_check_revoked,
    )
