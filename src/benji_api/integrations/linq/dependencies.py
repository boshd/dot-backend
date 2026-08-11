from typing import Annotated

from fastapi import Depends

from benji_api.config import Settings, get_settings
from benji_api.integrations.linq.client import LinqClient


def get_linq_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> LinqClient | None:
    if settings.linq_api_key is None:
        return None
    return LinqClient(
        api_key=settings.linq_api_key,
        base_url=settings.linq_api_base_url,
        timeout_seconds=settings.linq_request_timeout_seconds,
    )
