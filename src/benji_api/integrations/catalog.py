from dataclasses import dataclass
from typing import Literal

IntegrationAvailability = Literal["available", "coming_soon"]

GOOGLE_IDENTITY_SCOPES = (
    "openid",
    "email",
    "profile",
)


@dataclass(frozen=True, slots=True)
class IntegrationDefinition:
    key: str
    provider: str
    name: str
    description: str
    category: str
    availability: IntegrationAvailability
    required_scopes: tuple[str, ...] = ()
    webhook_kind: str | None = None


INTEGRATIONS = (
    IntegrationDefinition(
        key="google_calendar",
        provider="google",
        name="Google Calendar",
        description="Plan around your calendars and help manage events.",
        category="productivity",
        availability="available",
        required_scopes=(
            *GOOGLE_IDENTITY_SCOPES,
            "https://www.googleapis.com/auth/calendar.events",
        ),
        webhook_kind="google_calendar",
    ),
    IntegrationDefinition(
        key="gmail",
        provider="google",
        name="Gmail",
        description="Find, summarize, organize, and eventually send email with permission.",
        category="communication",
        availability="available",
        required_scopes=(
            *GOOGLE_IDENTITY_SCOPES,
            "https://www.googleapis.com/auth/gmail.modify",
        ),
        webhook_kind="gmail",
    ),
    IntegrationDefinition(
        key="plaid",
        provider="plaid",
        name="Plaid",
        description="Connect financial accounts for budgets, spending, and planning.",
        category="finance",
        availability="available",
    ),
    IntegrationDefinition(
        key="google_drive",
        provider="google",
        name="Google Drive",
        description="Work with the documents and files you choose.",
        category="productivity",
        availability="coming_soon",
    ),
    IntegrationDefinition(
        key="slack",
        provider="slack",
        name="Slack",
        description="Catch up, search conversations, and help coordinate work.",
        category="communication",
        availability="coming_soon",
    ),
)

_BY_KEY = {integration.key: integration for integration in INTEGRATIONS}


def get_integration(integration_key: str) -> IntegrationDefinition | None:
    return _BY_KEY.get(integration_key)
