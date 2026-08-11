import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.config import Settings
from benji_api.integrations.catalog import IntegrationDefinition, get_integration
from benji_api.integrations.google.client import (
    GoogleIntegrationClient,
    GoogleProviderError,
)
from benji_api.integrations.plaid.client import PlaidClient
from benji_api.models.integration import (
    IntegrationAccount,
    IntegrationConnectLink,
    IntegrationGrant,
    IntegrationOAuthState,
    IntegrationStatus,
    IntegrationSubscription,
    IntegrationSubscriptionStatus,
)
from benji_api.models.user import utc_now
from benji_api.services.integration_credentials import IntegrationCredentialVault
from benji_api.services.user_events import enqueue_user_event


class IntegrationNotFoundError(LookupError):
    pass


class IntegrationNotConfiguredError(RuntimeError):
    pass


class IntegrationAuthorizationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IntegrationAuthorization:
    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IntegrationConnectLinkResult:
    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CompletedIntegration:
    definition: IntegrationDefinition
    account: IntegrationAccount
    grant: IntegrationGrant
    user_event_id: UUID
    redirect_after: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def plaid_connect_surface_url(settings: Settings, *, raw_token: str) -> str:
    """Keep the private capability token client-side until the dedicated flow consumes it."""
    fragment = urlencode({"token": raw_token})
    return f"{settings.web_app_url}/connect/plaid#{fragment}"


def build_google_integration_client(settings: Settings) -> GoogleIntegrationClient:
    if settings.google_oauth_client_id is None or settings.google_oauth_client_secret is None:
        raise IntegrationNotConfiguredError("Google integrations are not configured")
    if settings.integration_token_encryption_key is None:
        raise IntegrationNotConfiguredError("Integration credential encryption is not configured")
    return GoogleIntegrationClient(
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        redirect_uri=settings.google_oauth_redirect_uri,
    )


def _available_integration(integration_key: str) -> IntegrationDefinition:
    definition = get_integration(integration_key)
    if definition is None:
        raise IntegrationNotFoundError("Integration was not found")
    if definition.availability != "available":
        raise IntegrationNotFoundError("Integration is not available yet")
    return definition


async def create_oauth_authorization(
    session: AsyncSession,
    *,
    user_id: UUID,
    integration_key: str,
    initiated_channel: str,
    settings: Settings,
    google_client: GoogleIntegrationClient | None = None,
) -> IntegrationAuthorization:
    definition = _available_integration(integration_key)
    if definition.provider != "google":
        raise IntegrationNotConfiguredError("This provider is not implemented")
    client = google_client or build_google_integration_client(settings)

    raw_state = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.integration_oauth_state_ttl_minutes)
    redirect_after = f"{settings.web_app_url}/?tab=integrations"
    session.add(
        IntegrationOAuthState(
            user_id=user_id,
            integration_key=definition.key,
            provider=definition.provider,
            token_hash=_token_hash(raw_state),
            requested_scopes=list(definition.required_scopes),
            initiated_channel=initiated_channel,
            redirect_after=redirect_after,
            expires_at=expires_at,
        )
    )
    await session.commit()
    return IntegrationAuthorization(
        url=client.authorization_url(state=raw_state, scopes=definition.required_scopes),
        expires_at=expires_at,
    )


async def create_integration_connect_link(
    session: AsyncSession,
    *,
    user_id: UUID,
    integration_key: str,
    settings: Settings,
) -> IntegrationConnectLinkResult:
    definition = _available_integration(integration_key)
    if definition.provider == "google":
        build_google_integration_client(settings)
    elif definition.provider == "plaid":
        from benji_api.services.finance import build_plaid_client

        build_plaid_client(settings)
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(
        minutes=settings.integration_connect_link_ttl_minutes
    )
    session.add(
        IntegrationConnectLink(
            user_id=user_id,
            integration_key=integration_key,
            token_hash=_token_hash(raw_token),
            expires_at=expires_at,
        )
    )
    await session.commit()
    if definition.provider == "plaid":
        url = plaid_connect_surface_url(settings, raw_token=raw_token)
    else:
        url = f"{settings.public_api_url}/api/v1/integrations/connect/{raw_token}"
    return IntegrationConnectLinkResult(
        url=url,
        expires_at=expires_at,
    )


async def consume_connect_link(
    session: AsyncSession,
    *,
    raw_token: str,
    settings: Settings,
    google_client: GoogleIntegrationClient | None = None,
) -> IntegrationAuthorization:
    link = await session.scalar(
        select(IntegrationConnectLink).where(
            IntegrationConnectLink.token_hash == _token_hash(raw_token)
        )
    )
    now = datetime.now(UTC)
    if link is None or link.consumed_at is not None or _as_utc(link.expires_at) <= now:
        raise IntegrationAuthorizationError("This integration link is invalid or expired")
    link.consumed_at = now
    await session.commit()
    return await create_oauth_authorization(
        session,
        user_id=link.user_id,
        integration_key=link.integration_key,
        initiated_channel="messaging_link",
        settings=settings,
        google_client=google_client,
    )


async def inspect_connect_link(
    session: AsyncSession,
    *,
    raw_token: str,
) -> IntegrationConnectLink:
    link = await session.scalar(
        select(IntegrationConnectLink).where(
            IntegrationConnectLink.token_hash == _token_hash(raw_token)
        )
    )
    now = datetime.now(UTC)
    if link is None or link.consumed_at is not None or _as_utc(link.expires_at) <= now:
        raise IntegrationAuthorizationError("This integration link is invalid or expired")
    return link


async def consume_plaid_connect_link(
    session: AsyncSession,
    *,
    raw_token: str,
    settings: Settings,
    plaid_client: PlaidClient | None = None,
):
    link = await session.scalar(
        select(IntegrationConnectLink)
        .where(IntegrationConnectLink.token_hash == _token_hash(raw_token))
        .with_for_update()
    )
    now = datetime.now(UTC)
    if link is None or link.consumed_at is not None or _as_utc(link.expires_at) <= now:
        raise IntegrationAuthorizationError("This integration link is invalid or expired")
    definition = _available_integration(link.integration_key)
    if definition.provider != "plaid":
        raise IntegrationAuthorizationError("This is not a bank connection link")
    from benji_api.services.finance import create_plaid_link_token

    try:
        result = await create_plaid_link_token(
            session,
            user_id=link.user_id,
            initiated_channel="messaging_link",
            settings=settings,
            plaid_client=plaid_client,
            commit=False,
        )
        link.consumed_at = now
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return result


async def complete_google_oauth(
    session: AsyncSession,
    *,
    raw_state: str,
    code: str,
    settings: Settings,
    google_client: GoogleIntegrationClient | None = None,
) -> CompletedIntegration:
    client = google_client or build_google_integration_client(settings)
    state = await session.scalar(
        select(IntegrationOAuthState).where(
            IntegrationOAuthState.token_hash == _token_hash(raw_state)
        )
    )
    now = datetime.now(UTC)
    if state is None or state.consumed_at is not None or _as_utc(state.expires_at) <= now:
        raise IntegrationAuthorizationError("Google authorization state is invalid or expired")
    definition = _available_integration(state.integration_key)
    if state.provider != "google" or definition.provider != "google":
        raise IntegrationAuthorizationError("Google authorization state is invalid")
    state.consumed_at = now
    await session.commit()

    tokens = await client.exchange_code(code)
    required_service_scope = definition.required_scopes[-1]
    if required_service_scope not in tokens.scopes:
        raise IntegrationAuthorizationError(f"{definition.name} permission was not granted")
    profile = await client.get_account_profile(tokens.access_token)
    if not profile.email_verified:
        raise IntegrationAuthorizationError("Google account email is not verified")

    account = await session.scalar(
        select(IntegrationAccount).where(
            IntegrationAccount.provider == "google",
            IntegrationAccount.provider_account_id == profile.account_id,
        )
    )
    if account is not None and account.user_id != state.user_id:
        raise IntegrationAuthorizationError(
            "This Google account is already connected to another Dot user"
        )
    vault = IntegrationCredentialVault(settings.integration_token_encryption_key or "")
    previous_credentials = (
        vault.decrypt(account.credentials_ciphertext) if account is not None else {}
    )
    refresh_token = tokens.refresh_token or previous_credentials.get("refresh_token")
    if not isinstance(refresh_token, str):
        raise IntegrationAuthorizationError(
            "Google did not issue offline access; reconnect and approve access"
        )
    credentials = {
        "access_token": tokens.access_token,
        "refresh_token": refresh_token,
    }
    merged_scopes = sorted(
        set(tokens.scopes) | set(account.granted_scopes if account is not None else [])
    )
    if account is None:
        account = IntegrationAccount(
            user_id=state.user_id,
            provider="google",
            provider_account_id=profile.account_id,
            email=profile.email,
            display_name=profile.display_name,
            credentials_ciphertext=vault.encrypt(credentials),
            granted_scopes=merged_scopes,
            token_expires_at=tokens.expires_at,
            metadata_json={"avatar_url": profile.avatar_url},
        )
        session.add(account)
        await session.flush()
    else:
        account.email = profile.email
        account.display_name = profile.display_name
        account.credentials_ciphertext = vault.encrypt(credentials)
        account.granted_scopes = merged_scopes
        account.token_expires_at = tokens.expires_at
        account.metadata_json = {"avatar_url": profile.avatar_url}
        account.status = IntegrationStatus.ACTIVE.value
        account.last_connected_at = now

    grant = await session.scalar(
        select(IntegrationGrant).where(
            IntegrationGrant.account_id == account.id,
            IntegrationGrant.integration_key == definition.key,
        )
    )
    if grant is None:
        grant = IntegrationGrant(account_id=account.id, integration_key=definition.key)
        session.add(grant)
    else:
        grant.status = IntegrationStatus.ACTIVE.value
        grant.connected_at = now
    user_event = await enqueue_user_event(
        session,
        user_id=state.user_id,
        event_type="integration.connected",
        source="integration_oauth",
        idempotency_key=f"integration.connected:{state.id}",
        payload={
            "integration_key": definition.key,
            "provider": definition.provider,
            "account_id": str(account.id),
            "account_email": account.email,
        },
        delivery_provider=("linq" if state.initiated_channel == "messaging_link" else None),
    )
    await session.commit()

    await _configure_subscription(
        session,
        account=account,
        definition=definition,
        access_token=tokens.access_token,
        settings=settings,
        google_client=client,
    )
    return CompletedIntegration(
        definition=definition,
        account=account,
        grant=grant,
        user_event_id=user_event.id,
        redirect_after=state.redirect_after or settings.web_app_url,
    )


async def _configure_subscription(
    session: AsyncSession,
    *,
    account: IntegrationAccount,
    definition: IntegrationDefinition,
    access_token: str,
    settings: Settings,
    google_client: GoogleIntegrationClient,
) -> None:
    subscription = await session.scalar(
        select(IntegrationSubscription).where(
            IntegrationSubscription.account_id == account.id,
            IntegrationSubscription.integration_key == definition.key,
        )
    )
    if subscription is None:
        subscription = IntegrationSubscription(
            account_id=account.id,
            integration_key=definition.key,
            provider="google",
            provider_subscription_id=f"pending:{uuid4()}",
        )
        session.add(subscription)

    try:
        if definition.key == "google_calendar":
            if settings.google_calendar_webhook_url is None:
                subscription.status = IntegrationSubscriptionStatus.PENDING_CONFIGURATION.value
                subscription.metadata_json = {
                    "reason": "GOOGLE_CALENDAR_WEBHOOK_URL is not configured"
                }
                await session.commit()
                return
            channel_id = str(uuid4())
            verification_token = secrets.token_urlsafe(32)
            subscription.provider_subscription_id = channel_id
            subscription.verification_token_hash = _token_hash(verification_token)
            subscription.status = IntegrationSubscriptionStatus.ACTIVE.value
            await session.commit()
            provider_subscription = await google_client.watch_calendar(
                access_token=access_token,
                channel_id=channel_id,
                webhook_url=settings.google_calendar_webhook_url,
                verification_token=verification_token,
            )
        elif definition.key == "gmail":
            if settings.google_gmail_pubsub_topic is None:
                subscription.status = IntegrationSubscriptionStatus.PENDING_CONFIGURATION.value
                subscription.metadata_json = {
                    "reason": "GOOGLE_GMAIL_PUBSUB_TOPIC is not configured"
                }
                await session.commit()
                return
            gmail_subscription_id = f"gmail:{account.id}"
            subscription.provider_subscription_id = gmail_subscription_id
            subscription.status = IntegrationSubscriptionStatus.ACTIVE.value
            await session.commit()
            provider_subscription = await google_client.watch_gmail(
                access_token=access_token,
                topic_name=settings.google_gmail_pubsub_topic,
                subscription_id=gmail_subscription_id,
            )
        else:
            return
    except GoogleProviderError as error:
        subscription.status = IntegrationSubscriptionStatus.FAILED.value
        subscription.metadata_json = {"error": str(error)[:500]}
    else:
        subscription.provider_subscription_id = provider_subscription.subscription_id
        subscription.provider_resource_id = provider_subscription.resource_id
        subscription.cursor = provider_subscription.cursor
        subscription.expires_at = provider_subscription.expires_at
        subscription.status = IntegrationSubscriptionStatus.ACTIVE.value
        subscription.metadata_json = {}
    await session.commit()


async def get_valid_google_access_token(
    session: AsyncSession,
    *,
    account: IntegrationAccount,
    settings: Settings,
    google_client: GoogleIntegrationClient | None = None,
) -> str:
    if account.provider != "google":
        raise IntegrationAuthorizationError("Account is not a Google integration")
    if settings.integration_token_encryption_key is None:
        raise IntegrationNotConfiguredError("Integration credential encryption is not configured")
    vault = IntegrationCredentialVault(settings.integration_token_encryption_key)
    credentials = vault.decrypt(account.credentials_ciphertext)
    access_token = credentials.get("access_token")
    if (
        isinstance(access_token, str)
        and account.token_expires_at is not None
        and _as_utc(account.token_expires_at) > datetime.now(UTC) + timedelta(seconds=60)
    ):
        return access_token
    refresh_token = credentials.get("refresh_token")
    if not isinstance(refresh_token, str):
        account.status = IntegrationStatus.NEEDS_REAUTHORIZATION.value
        await session.commit()
        raise IntegrationAuthorizationError("Google account needs to be reconnected")
    client = google_client or build_google_integration_client(settings)
    refreshed = await client.refresh_access_token(refresh_token)
    credentials["access_token"] = refreshed.access_token
    account.credentials_ciphertext = vault.encrypt(credentials)
    account.token_expires_at = refreshed.expires_at
    account.updated_at = utc_now()
    await session.commit()
    return refreshed.access_token


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
