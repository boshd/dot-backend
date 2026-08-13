import base64
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.tools import (
    DisconnectGoogleIntegrationTool,
    GetGmailMessageTool,
    GoogleCalendarEventsTool,
    ListConnectedIntegrationsTool,
    SearchGmailTool,
    build_default_tool_registry,
)
from benji_api.agents.types import ToolContext
from benji_api.config import Settings, get_settings
from benji_api.db.base import Base
from benji_api.db.session import get_session
from benji_api.integrations.google.client import _gmail_message
from benji_api.integrations.types import (
    CalendarEvent,
    CalendarEventPage,
    GmailMessage,
    GmailMessagePage,
    GmailMessageSummary,
    OAuthTokenSet,
    ProviderAccountProfile,
    ProviderSubscription,
)
from benji_api.main import app
from benji_api.models import (
    Conversation,
    IntegrationAccount,
    IntegrationGrant,
    IntegrationSubscription,
    User,
    UserEvent,
    WebhookEvent,
)
from benji_api.services.integration_credentials import IntegrationCredentialVault
from benji_api.services.integrations import (
    complete_google_oauth,
    create_oauth_authorization,
    oauth_callback_redirect_url,
)

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def test_gmail_message_parser_extracts_headers_and_safe_text_body() -> None:
    encoded_body = base64.urlsafe_b64encode(
        b"The launch is Friday.\nPlease bring the final checklist."
    ).decode()
    message = _gmail_message(
        {
            "id": "message-one",
            "threadId": "thread-one",
            "snippet": "The launch is Friday.",
            "labelIds": ["INBOX", "IMPORTANT"],
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "From", "value": "Alex <alex@example.com>"},
                    {"name": "To", "value": "one@example.com"},
                    {"name": "Cc", "value": "team@example.com"},
                    {"name": "Subject", "value": "Project update"},
                    {"name": "Date", "value": "Sun, 9 Aug 2026 18:00:00 +0300"},
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": encoded_body},
                    }
                ],
            },
        },
        max_body_chars=24,
    )

    assert message.subject == "Project update"
    assert message.recipients == ("one@example.com",)
    assert message.cc == ("team@example.com",)
    assert message.body_text.startswith("The launch is Friday.")
    assert message.body_text.endswith("…")
    assert message.body_truncated is True


class FakeGoogleClient:
    def __init__(self) -> None:
        self.last_code = ""
        self.calendar_tokens: dict[str, str] = {}
        self.stopped_calendar_channels: list[tuple[str, str]] = []
        self.gmail_stop_count = 0
        self.revoked_tokens: list[str] = []

    def authorization_url(self, *, state: str, scopes: tuple[str, ...]) -> str:
        return f"https://accounts.example/authorize?state={state}&scope={' '.join(scopes)}"

    async def exchange_code(self, code: str) -> OAuthTokenSet:
        self.last_code = code
        service_scope = GMAIL_SCOPE if "gmail" in code else CALENDAR_SCOPE
        return OAuthTokenSet(
            access_token=f"access-{code}",
            refresh_token=f"refresh-{code}",
            scopes=("openid", service_scope),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async def get_account_profile(self, access_token: str) -> ProviderAccountProfile:
        suffix = "two" if "account-two" in access_token else "one"
        return ProviderAccountProfile(
            account_id=f"google-account-{suffix}",
            email=f"{suffix}@example.com",
            display_name=f"Account {suffix.title()}",
            avatar_url=None,
            email_verified=True,
        )

    async def watch_calendar(
        self,
        *,
        access_token: str,
        channel_id: str,
        webhook_url: str,
        verification_token: str,
    ) -> ProviderSubscription:
        del access_token, webhook_url
        self.calendar_tokens[channel_id] = verification_token
        return ProviderSubscription(
            subscription_id=channel_id,
            resource_id="calendar-resource",
            cursor=None,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )

    async def watch_gmail(
        self,
        *,
        access_token: str,
        topic_name: str,
        subscription_id: str,
    ) -> ProviderSubscription:
        del access_token, topic_name
        return ProviderSubscription(
            subscription_id=subscription_id,
            resource_id=None,
            cursor="100",
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )

    async def stop_calendar_watch(
        self,
        *,
        access_token: str,
        channel_id: str,
        resource_id: str,
    ) -> None:
        del access_token
        self.stopped_calendar_channels.append((channel_id, resource_id))

    async def stop_gmail_watch(self, *, access_token: str) -> None:
        del access_token
        self.gmail_stop_count += 1

    async def revoke_token(self, token: str) -> None:
        self.revoked_tokens.append(token)

    async def list_calendar_events(
        self,
        *,
        access_token: str,
        time_min: datetime,
        time_max: datetime,
        time_zone: str,
        max_results: int = 100,
    ) -> CalendarEventPage:
        del time_min, time_max, max_results
        return CalendarEventPage(
            events=(
                CalendarEvent(
                    event_id=f"event-{access_token}",
                    title="Planning",
                    start="2026-08-10T10:00:00+03:00",
                    end="2026-08-10T11:00:00+03:00",
                    all_day=False,
                    status="confirmed",
                    location=None,
                    organizer_email=None,
                    attendee_count=0,
                    html_link=None,
                ),
            ),
            truncated=False,
            calendar_timezone=time_zone,
        )

    async def search_gmail_messages(
        self,
        *,
        access_token: str,
        query: str,
        max_results: int = 10,
    ) -> GmailMessagePage:
        del max_results
        return GmailMessagePage(
            messages=(
                GmailMessageSummary(
                    message_id=f"message-{access_token}",
                    thread_id="thread-one",
                    subject="Project update",
                    sender="Alex <alex@example.com>",
                    recipients=("one@example.com",),
                    sent_at="Sun, 9 Aug 2026 18:00:00 +0300",
                    snippet=f"Here is the latest project update matching {query}",
                    labels=("INBOX",),
                ),
            ),
            truncated=False,
        )

    async def get_gmail_message(
        self,
        *,
        access_token: str,
        message_id: str,
        max_body_chars: int = 12_000,
    ) -> GmailMessage:
        del max_body_chars
        return GmailMessage(
            message_id=message_id,
            thread_id="thread-one",
            subject="Project update",
            sender="Alex <alex@example.com>",
            recipients=("one@example.com",),
            cc=(),
            sent_at="Sun, 9 Aug 2026 18:00:00 +0300",
            snippet="Here is the latest project update",
            labels=("INBOX",),
            body_text=f"The launch is Friday. token={access_token[:6]}…",
            body_truncated=False,
        )


@pytest.mark.anyio
async def test_messaging_google_connect_returns_to_an_unauthenticated_done_page() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        web_app_url="https://app.textdot.test",
        integration_token_encryption_key=Fernet.generate_key().decode(),
        google_oauth_client_id="google-client",
        google_oauth_client_secret="google-secret",
    )
    fake_google = FakeGoogleClient()

    async with session_factory() as session:
        user = User(phone_number="+14155552671")
        session.add(user)
        await session.commit()
        authorization = await create_oauth_authorization(
            session,
            user_id=user.id,
            integration_key="google_calendar",
            initiated_channel="messaging_link",
            settings=settings,
            google_client=fake_google,
        )
        state = parse_qs(urlparse(authorization.url).query)["state"][0]
        completed = await complete_google_oauth(
            session,
            raw_state=state,
            code="calendar-account-one",
            settings=settings,
            google_client=fake_google,
        )

    assert completed.redirect_after == "https://app.textdot.test/connect/done"
    assert oauth_callback_redirect_url(
        settings,
        completed.redirect_after,
        connected="google_calendar",
        account=completed.account.email,
    ).startswith("https://app.textdot.test/connect/done?")
    assert "/?tab=integrations" not in oauth_callback_redirect_url(
        settings,
        completed.redirect_after,
        connected="google_calendar",
        account=completed.account.email,
    )
    assert oauth_callback_redirect_url(
        settings,
        "https://evil.example/phish",
        connected="google_calendar",
    ).startswith("https://app.textdot.test/")

    await engine.dispose()


@pytest.mark.anyio
async def test_google_multi_account_connections_and_webhooks() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        web_chat_dev_identity_enabled=True,
        integration_token_encryption_key=Fernet.generate_key().decode(),
        google_oauth_client_id="google-client",
        google_oauth_client_secret="google-secret",
        google_calendar_webhook_url="https://api.example.com/calendar",
        google_gmail_pubsub_topic="projects/test/topics/gmail",
        google_pubsub_push_verification_token="pubsub-secret",
    )
    fake_google = FakeGoogleClient()
    phone = "+14155552671"

    async with session_factory() as session:
        user = User(phone_number=phone)
        session.add(user)
        await session.commit()

        calendar_authorization = await create_oauth_authorization(
            session,
            user_id=user.id,
            integration_key="google_calendar",
            initiated_channel="messaging_link",
            settings=settings,
            google_client=fake_google,
        )
        calendar_state = parse_qs(urlparse(calendar_authorization.url).query)["state"][0]
        await complete_google_oauth(
            session,
            raw_state=calendar_state,
            code="calendar-account-one",
            settings=settings,
            google_client=fake_google,
        )

        gmail_authorization = await create_oauth_authorization(
            session,
            user_id=user.id,
            integration_key="gmail",
            initiated_channel="web",
            settings=settings,
            google_client=fake_google,
        )
        gmail_state = parse_qs(urlparse(gmail_authorization.url).query)["state"][0]
        await complete_google_oauth(
            session,
            raw_state=gmail_state,
            code="gmail-account-one",
            settings=settings,
            google_client=fake_google,
        )

        second_authorization = await create_oauth_authorization(
            session,
            user_id=user.id,
            integration_key="google_calendar",
            initiated_channel="web",
            settings=settings,
            google_client=fake_google,
        )
        second_state = parse_qs(urlparse(second_authorization.url).query)["state"][0]
        await complete_google_oauth(
            session,
            raw_state=second_state,
            code="calendar-account-two",
            settings=settings,
            google_client=fake_google,
        )

        accounts = list((await session.scalars(select(IntegrationAccount))).all())
        grants = list((await session.scalars(select(IntegrationGrant))).all())
        subscriptions = list((await session.scalars(select(IntegrationSubscription))).all())
        assert len(accounts) == 2
        assert len(grants) == 3
        assert len(subscriptions) == 3
        assert await session.scalar(select(func.count()).select_from(UserEvent)) == 3
        assert all("access-" not in account.credentials_ciphertext for account in accounts)
        first_account = next(account for account in accounts if account.email == "one@example.com")
        calendar_subscription = next(
            subscription
            for subscription in subscriptions
            if subscription.account_id == first_account.id
            and subscription.integration_key == "google_calendar"
        )

        calendar_tool = GoogleCalendarEventsTool(
            settings,
            google_client=fake_google,  # type: ignore[arg-type]
            session_factory=session_factory,
        )
        calendar_result = await calendar_tool.execute(
            context=ToolContext(user_id=user.id, conversation_id=user.id),
            arguments={
                "time_min": "2026-08-10T00:00:00+03:00",
                "time_max": "2026-08-17T00:00:00+03:00",
                "timezone": "Africa/Cairo",
                "account_email": None,
            },
        )
        assert calendar_result["connected"] is True
        assert calendar_result["event_count"] == 2
        assert {account["email"] for account in calendar_result["accounts"]} == {
            "one@example.com",
            "two@example.com",
        }

        connected_tool = ListConnectedIntegrationsTool(session_factory=session_factory)
        connected_result = await connected_tool.execute(
            context=ToolContext(user_id=user.id, conversation_id=user.id),
            arguments={},
        )
        connected_by_key = {
            integration["key"]: integration for integration in connected_result["integrations"]
        }
        assert {item["email"] for item in connected_by_key["google_calendar"]["accounts"]} == {
            "one@example.com",
            "two@example.com",
        }
        assert [item["email"] for item in connected_by_key["gmail"]["accounts"]] == [
            "one@example.com"
        ]
        assert connected_by_key["gmail"]["accounts"][0]["account_id"] == str(first_account.id)

        search_tool = SearchGmailTool(
            settings,
            google_client=fake_google,  # type: ignore[arg-type]
            session_factory=session_factory,
        )
        search_result = await search_tool.execute(
            context=ToolContext(user_id=user.id, conversation_id=user.id),
            arguments={
                "query": "from:alex newer_than:7d",
                "account_email": None,
                "max_results": 5,
            },
        )
        assert search_result["connected"] is True
        assert search_result["message_count"] == 1
        assert search_result["accounts"][0]["email"] == "one@example.com"
        message_id = search_result["accounts"][0]["messages"][0]["id"]

        message_tool = GetGmailMessageTool(
            settings,
            google_client=fake_google,  # type: ignore[arg-type]
            session_factory=session_factory,
        )
        message_result = await message_tool.execute(
            context=ToolContext(user_id=user.id, conversation_id=user.id),
            arguments={"account_email": "one@example.com", "message_id": message_id},
        )
        assert message_result["message"]["body"].startswith("The launch is Friday.")

        tool_names = [
            definition.name for definition in build_default_tool_registry(settings).definitions()
        ]
        assert tool_names == [
            "get_current_datetime",
            "list_connected_integrations",
            "schedule_proactive_reachout",
            "list_scheduled_reachouts",
            "cancel_scheduled_reachout",
            "get_financial_overview",
            "search_financial_transactions",
            "create_financial_goal",
            "list_financial_goals",
            "cancel_financial_goal",
            "get_account_settings",
            "update_account_setting",
            "delete_dot_account",
            "cancel_account_deletion",
            "create_personal_app",
            "list_personal_apps",
            "inspect_custom_app",
            "create_custom_app_link",
            "list_custom_app_records",
            "add_custom_app_record",
            "update_custom_app_record",
            "delete_custom_app_record",
            "revise_custom_app",
            "rollback_custom_app",
            "delete_personal_app",
            "get_personal_app",
            "add_personal_app_record",
            "update_personal_app_record",
            "delete_personal_app_record",
            "create_integration_connect_link",
            "get_calendar_events",
            "search_gmail",
            "get_gmail_message",
            "disconnect_google_integration",
        ]

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            catalog = await client.post(
                "/api/v1/integrations/catalog", json={"phone_number": phone}
            )
            assert catalog.status_code == 200
            catalog_by_key = {item["key"]: item for item in catalog.json()["integrations"]}
            assert len(catalog_by_key["google_calendar"]["connections"]) == 2
            assert len(catalog_by_key["gmail"]["connections"]) == 1
            assert catalog_by_key["plaid"]["availability"] == "available"

            calendar_push = await client.post(
                "/api/v1/webhooks/google/calendar",
                headers={
                    "X-Goog-Channel-ID": calendar_subscription.provider_subscription_id,
                    "X-Goog-Resource-ID": "calendar-resource",
                    "X-Goog-Resource-State": "exists",
                    "X-Goog-Message-Number": "1",
                    "X-Goog-Channel-Token": fake_google.calendar_tokens[
                        calendar_subscription.provider_subscription_id
                    ],
                },
            )
            assert calendar_push.status_code == 204

            data = base64.urlsafe_b64encode(
                json.dumps({"emailAddress": "one@example.com", "historyId": "101"}).encode()
            ).decode()
            gmail_push = await client.post(
                "/api/v1/webhooks/google/gmail?token=pubsub-secret",
                json={"message": {"messageId": "pubsub-1", "data": data}},
            )
            assert gmail_push.status_code == 204
    finally:
        app.dependency_overrides.clear()

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 2
        gmail_subscription = await session.scalar(
            select(IntegrationSubscription).where(
                IntegrationSubscription.account_id == first_account.id,
                IntegrationSubscription.integration_key == "gmail",
            )
        )
        assert gmail_subscription is not None
        assert gmail_subscription.cursor == "101"
    await engine.dispose()


@pytest.mark.anyio
async def test_agent_disconnects_one_google_grant_without_breaking_the_other() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        integration_token_encryption_key=encryption_key,
        google_oauth_client_id="google-client",
        google_oauth_client_secret="google-secret",
    )
    fake_google = FakeGoogleClient()
    vault = IntegrationCredentialVault(encryption_key)
    async with session_factory() as session:
        user = User(phone_number="+14155552671")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        account = IntegrationAccount(
            user_id=user.id,
            provider="google",
            provider_account_id="google-account-one",
            email="one@example.com",
            credentials_ciphertext=vault.encrypt(
                {"access_token": "access-one", "refresh_token": "refresh-one"}
            ),
            granted_scopes=[CALENDAR_SCOPE, GMAIL_SCOPE],
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(account)
        await session.flush()
        session.add_all(
            [
                IntegrationGrant(account_id=account.id, integration_key="google_calendar"),
                IntegrationGrant(account_id=account.id, integration_key="gmail"),
                IntegrationSubscription(
                    account_id=account.id,
                    integration_key="google_calendar",
                    provider="google",
                    provider_subscription_id="calendar-channel",
                    provider_resource_id="calendar-resource",
                ),
                IntegrationSubscription(
                    account_id=account.id,
                    integration_key="gmail",
                    provider="google",
                    provider_subscription_id=f"gmail:{account.id}",
                ),
            ]
        )
        await session.commit()

    tool = DisconnectGoogleIntegrationTool(
        settings,
        google_client=fake_google,  # type: ignore[arg-type]
        session_factory=session_factory,
    )
    gmail_result = await tool.execute(
        context=ToolContext(user_id=user.id, conversation_id=conversation.id),
        arguments={"integration": "gmail", "account_id": str(account.id)},
    )
    assert gmail_result["disconnected"] is True
    assert gmail_result["provider_access_revoked"] is False
    assert fake_google.gmail_stop_count == 1
    assert fake_google.revoked_tokens == []

    calendar_result = await tool.execute(
        context=ToolContext(user_id=user.id, conversation_id=conversation.id),
        arguments={"integration": "google_calendar", "account_id": str(account.id)},
    )
    assert calendar_result["disconnected"] is True
    assert calendar_result["provider_access_revoked"] is True
    assert fake_google.stopped_calendar_channels == [("calendar-channel", "calendar-resource")]
    assert fake_google.revoked_tokens == ["refresh-one"]
    async with session_factory() as session:
        assert await session.get(IntegrationAccount, account.id) is None
        assert await session.scalar(select(func.count()).select_from(IntegrationGrant)) == 0
        assert await session.scalar(select(func.count()).select_from(IntegrationSubscription)) == 0
    await engine.dispose()
