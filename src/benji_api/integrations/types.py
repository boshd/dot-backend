from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class OAuthTokenSet:
    access_token: str
    refresh_token: str | None
    scopes: tuple[str, ...]
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProviderAccountProfile:
    account_id: str
    email: str
    display_name: str | None
    avatar_url: str | None
    email_verified: bool


@dataclass(frozen=True, slots=True)
class ProviderSubscription:
    subscription_id: str
    resource_id: str | None
    cursor: str | None
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    event_id: str
    title: str
    start: str
    end: str
    all_day: bool
    status: str
    location: str | None
    organizer_email: str | None
    attendee_count: int
    html_link: str | None


@dataclass(frozen=True, slots=True)
class CalendarEventPage:
    events: tuple[CalendarEvent, ...]
    truncated: bool
    calendar_timezone: str | None


@dataclass(frozen=True, slots=True)
class GmailMessageSummary:
    message_id: str
    thread_id: str
    subject: str
    sender: str | None
    recipients: tuple[str, ...]
    sent_at: str | None
    snippet: str
    labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GmailMessagePage:
    messages: tuple[GmailMessageSummary, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class GmailMessage:
    message_id: str
    thread_id: str
    subject: str
    sender: str | None
    recipients: tuple[str, ...]
    cc: tuple[str, ...]
    sent_at: str | None
    snippet: str
    labels: tuple[str, ...]
    body_text: str
    body_truncated: bool
