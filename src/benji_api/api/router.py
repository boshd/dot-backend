from fastapi import APIRouter

from benji_api.api.routes import (
    auth,
    generated_apps,
    generated_apps_v2,
    groups,
    health,
    inbound_messages,
    integrations,
    linq_webhooks,
    messages,
    waitlist,
    web_chat,
)
from benji_api.config import Settings, get_settings


def build_api_router(settings: Settings | None = None) -> APIRouter:
    runtime_settings = settings or get_settings()
    router = APIRouter()
    router.include_router(health.router)
    router.include_router(auth.router, prefix="/api/v1")
    router.include_router(generated_apps.router, prefix="/api/v1")
    router.include_router(generated_apps_v2.router, prefix="/api/v1")
    router.include_router(groups.router, prefix="/api/v1")
    router.include_router(integrations.router, prefix="/api/v1")
    router.include_router(integrations.webhook_router, prefix="/api/v1")
    router.include_router(integrations.plaid_webhook_router, prefix="/api/v1")
    router.include_router(linq_webhooks.router, prefix="/api/v1")
    router.include_router(waitlist.router, prefix="/api/v1")
    router.include_router(web_chat.router, prefix="/api/v1")
    if runtime_settings.environment.casefold() != "production":
        router.include_router(inbound_messages.router, prefix="/api/v1")
        router.include_router(messages.router, prefix="/api/v1")
    return router


api_router = build_api_router()
