import base64
import binascii
import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.api.dependencies import get_optional_authenticated_user, resolve_client_user
from benji_api.config import Settings, get_settings
from benji_api.db.session import get_session
from benji_api.integrations.catalog import INTEGRATIONS, get_integration
from benji_api.integrations.google.client import GoogleProviderError
from benji_api.integrations.linq.client import LinqClient
from benji_api.integrations.linq.dependencies import get_linq_client
from benji_api.integrations.plaid.client import PlaidProviderError
from benji_api.models.channel import WebhookEvent, WebhookStatus
from benji_api.models.finance import (
    FinancialAccount,
    FinancialConnection,
    FinancialConnectionStatus,
)
from benji_api.models.integration import (
    IntegrationAccount,
    IntegrationGrant,
    IntegrationSubscription,
)
from benji_api.models.user import User
from benji_api.schemas.phone import PhoneNumber
from benji_api.services.finance import (
    FinancialAuthorizationError,
    FinancialIntegrationNotConfiguredError,
    build_plaid_client,
    complete_plaid_link,
    create_plaid_link_token,
    disconnect_financial_connection,
    enqueue_plaid_sync,
)
from benji_api.services.integrations import (
    IntegrationAuthorizationError,
    IntegrationNotConfiguredError,
    IntegrationNotFoundError,
    complete_google_oauth,
    consume_connect_link,
    consume_plaid_connect_link,
    create_oauth_authorization,
    inspect_connect_link,
)
from benji_api.services.user_events import dispatch_user_event, enqueue_user_event

router = APIRouter(prefix="/integrations", tags=["integrations"])
webhook_router = APIRouter(prefix="/webhooks/google", tags=["google webhooks"])
plaid_webhook_router = APIRouter(prefix="/webhooks/plaid", tags=["plaid webhooks"])


class IntegrationCatalogRequest(BaseModel):
    phone_number: PhoneNumber | None = None


class IntegrationConnectionResponse(BaseModel):
    account_id: UUID
    email: str | None
    label: str
    display_name: str | None
    status: str
    account_count: int = 1
    subscription_status: str | None
    subscription_expires_at: datetime | None


class IntegrationCatalogItemResponse(BaseModel):
    key: str
    provider: str
    name: str
    description: str
    category: str
    availability: str
    connections: list[IntegrationConnectionResponse]


class IntegrationCatalogResponse(BaseModel):
    integrations: list[IntegrationCatalogItemResponse]


class IntegrationConnectRequest(BaseModel):
    phone_number: PhoneNumber | None = None


class IntegrationConnectResponse(BaseModel):
    flow: str = "redirect"
    authorization_url: str | None = None
    link_token: str | None = None
    exchange_token: str | None = None
    expires_at: datetime


class PlaidConnectLinkRequest(BaseModel):
    connect_token: str


class PlaidExchangeRequest(BaseModel):
    public_token: str
    exchange_token: str
    institution_id: str | None = None
    institution_name: str | None = None


class PlaidExchangeResponse(BaseModel):
    connection_id: UUID
    institution_name: str
    sync_status: str


class FinancialDisconnectResponse(BaseModel):
    disconnected: bool


@router.post("/catalog", response_model=IntegrationCatalogResponse)
async def integration_catalog(
    request: IntegrationCatalogRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
) -> IntegrationCatalogResponse:
    user = await resolve_client_user(
        session,
        authenticated_user=authenticated_user,
        phone_number=request.phone_number,
        settings=settings,
    )
    accounts = list(
        (
            await session.scalars(
                select(IntegrationAccount).where(IntegrationAccount.user_id == user.id)
            )
        ).all()
    )
    account_ids = [account.id for account in accounts]
    grants = (
        list(
            (
                await session.scalars(
                    select(IntegrationGrant).where(IntegrationGrant.account_id.in_(account_ids))
                )
            ).all()
        )
        if account_ids
        else []
    )
    subscriptions = (
        list(
            (
                await session.scalars(
                    select(IntegrationSubscription).where(
                        IntegrationSubscription.account_id.in_(account_ids)
                    )
                )
            ).all()
        )
        if account_ids
        else []
    )
    financial_connections = list(
        (
            await session.scalars(
                select(FinancialConnection).where(FinancialConnection.user_id == user.id)
            )
        ).all()
    )
    financial_connection_ids = [connection.id for connection in financial_connections]
    financial_accounts = (
        list(
            (
                await session.scalars(
                    select(FinancialAccount).where(
                        FinancialAccount.connection_id.in_(financial_connection_ids)
                    )
                )
            ).all()
        )
        if financial_connection_ids
        else []
    )
    financial_account_counts: dict[UUID, int] = {}
    for account in financial_accounts:
        financial_account_counts[account.connection_id] = (
            financial_account_counts.get(account.connection_id, 0) + 1
        )
    accounts_by_id = {account.id: account for account in accounts}
    subscription_by_account_and_key = {
        (subscription.account_id, subscription.integration_key): subscription
        for subscription in subscriptions
    }

    items: list[IntegrationCatalogItemResponse] = []
    for definition in INTEGRATIONS:
        connections: list[IntegrationConnectionResponse] = []
        for grant in grants:
            if grant.integration_key != definition.key:
                continue
            account = accounts_by_id[grant.account_id]
            subscription = subscription_by_account_and_key.get((account.id, definition.key))
            connections.append(
                IntegrationConnectionResponse(
                    account_id=account.id,
                    email=account.email,
                    label=account.email,
                    display_name=account.display_name,
                    status=grant.status,
                    subscription_status=(subscription.status if subscription else None),
                    subscription_expires_at=(subscription.expires_at if subscription else None),
                )
            )
        if definition.provider == "plaid":
            connections.extend(
                IntegrationConnectionResponse(
                    account_id=connection.id,
                    email=None,
                    label=connection.institution_name,
                    display_name=connection.institution_name,
                    status=connection.status,
                    account_count=financial_account_counts.get(connection.id, 0),
                    subscription_status=connection.sync_status,
                    subscription_expires_at=connection.consent_expires_at,
                )
                for connection in financial_connections
                if connection.status != FinancialConnectionStatus.REVOKED.value
            )
        items.append(
            IntegrationCatalogItemResponse(
                key=definition.key,
                provider=definition.provider,
                name=definition.name,
                description=definition.description,
                category=definition.category,
                availability=definition.availability,
                connections=connections,
            )
        )
    return IntegrationCatalogResponse(integrations=items)


@router.post("/{integration_key}/connect", response_model=IntegrationConnectResponse)
async def connect_integration(
    integration_key: str,
    request: IntegrationConnectRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
) -> IntegrationConnectResponse:
    user = await resolve_client_user(
        session,
        authenticated_user=authenticated_user,
        phone_number=request.phone_number,
        settings=settings,
    )
    definition = get_integration(integration_key)
    if definition is not None and definition.provider == "plaid":
        try:
            link = await create_plaid_link_token(
                session,
                user_id=user.id,
                initiated_channel="web",
                settings=settings,
            )
        except (
            FinancialIntegrationNotConfiguredError,
            PlaidProviderError,
        ) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error
        return IntegrationConnectResponse(
            flow="plaid_link",
            link_token=link.link_token,
            exchange_token=link.exchange_token,
            expires_at=link.expires_at,
        )
    try:
        authorization = await create_oauth_authorization(
            session,
            user_id=user.id,
            integration_key=integration_key,
            initiated_channel="web",
            settings=settings,
        )
    except IntegrationNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except IntegrationNotConfiguredError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    return IntegrationConnectResponse(
        flow="redirect",
        authorization_url=authorization.url,
        expires_at=authorization.expires_at,
    )


@router.get("/connect/{token}", include_in_schema=False)
async def open_integration_connect_link(
    token: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    try:
        link = await inspect_connect_link(session, raw_token=token)
        definition = get_integration(link.integration_key)
        if definition is not None and definition.provider == "plaid":
            query = urlencode(
                {
                    "tab": "integrations",
                    "connect": "plaid",
                    "connect_token": token,
                }
            )
            return RedirectResponse(
                f"{settings.web_app_url}/?{query}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        authorization = await consume_connect_link(
            session,
            raw_token=token,
            settings=settings,
        )
    except (IntegrationAuthorizationError, IntegrationNotConfiguredError) as error:
        return _web_redirect(settings, integration_error=str(error))
    return RedirectResponse(authorization.url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.post("/plaid/link-token/from-connect-link", response_model=IntegrationConnectResponse)
async def plaid_link_token_from_connect_link(
    request: PlaidConnectLinkRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> IntegrationConnectResponse:
    try:
        link = await consume_plaid_connect_link(
            session,
            raw_token=request.connect_token,
            settings=settings,
        )
    except (
        IntegrationAuthorizationError,
        FinancialIntegrationNotConfiguredError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return IntegrationConnectResponse(
        flow="plaid_link",
        link_token=link.link_token,
        exchange_token=link.exchange_token,
        expires_at=link.expires_at,
    )


@router.post("/plaid/{connection_id}/reconnect", response_model=IntegrationConnectResponse)
async def reconnect_plaid_connection(
    connection_id: UUID,
    request: IntegrationConnectRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
) -> IntegrationConnectResponse:
    user = await resolve_client_user(
        session,
        authenticated_user=authenticated_user,
        phone_number=request.phone_number,
        settings=settings,
    )
    try:
        link = await create_plaid_link_token(
            session,
            user_id=user.id,
            initiated_channel="web",
            settings=settings,
            connection_id=connection_id,
        )
    except (
        FinancialAuthorizationError,
        FinancialIntegrationNotConfiguredError,
        PlaidProviderError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return IntegrationConnectResponse(
        flow="plaid_link",
        link_token=link.link_token,
        exchange_token=link.exchange_token,
        expires_at=link.expires_at,
    )


@router.post("/plaid/exchange", response_model=PlaidExchangeResponse)
async def exchange_plaid_public_token(
    request: PlaidExchangeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PlaidExchangeResponse:
    try:
        completed = await complete_plaid_link(
            session,
            exchange_token=request.exchange_token,
            public_token=request.public_token,
            institution_id=request.institution_id,
            institution_name=request.institution_name,
            settings=settings,
        )
    except (
        FinancialAuthorizationError,
        FinancialIntegrationNotConfiguredError,
        PlaidProviderError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return PlaidExchangeResponse(
        connection_id=completed.connection.id,
        institution_name=completed.connection.institution_name,
        sync_status=completed.connection.sync_status,
    )


@router.delete("/plaid/{connection_id}", response_model=FinancialDisconnectResponse)
async def disconnect_plaid_connection(
    connection_id: UUID,
    request: IntegrationConnectRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
) -> FinancialDisconnectResponse:
    user = await resolve_client_user(
        session,
        authenticated_user=authenticated_user,
        phone_number=request.phone_number,
        settings=settings,
    )
    try:
        disconnected = await disconnect_financial_connection(
            session,
            user_id=user.id,
            connection_id=connection_id,
            settings=settings,
        )
        await session.commit()
    except (
        FinancialAuthorizationError,
        FinancialIntegrationNotConfiguredError,
        PlaidProviderError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    if not disconnected:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Financial connection was not found",
        )
    return FinancialDisconnectResponse(disconnected=True)


@router.get("/google/callback", include_in_schema=False)
async def google_oauth_callback(
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    linq_client: Annotated[LinqClient | None, Depends(get_linq_client)],
    state_token: Annotated[str | None, Query(alias="state")] = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    if error is not None:
        return _web_redirect(settings, integration_error="Google access was not approved")
    if state_token is None or code is None:
        return _web_redirect(settings, integration_error="Google authorization was incomplete")
    try:
        completed = await complete_google_oauth(
            session,
            raw_state=state_token,
            code=code,
            settings=settings,
        )
    except (
        GoogleProviderError,
        IntegrationAuthorizationError,
        IntegrationNotConfiguredError,
        IntegrationNotFoundError,
    ) as callback_error:
        return _web_redirect(settings, integration_error=str(callback_error))
    background_tasks.add_task(
        dispatch_user_event,
        settings=settings,
        linq_client=linq_client,
        event_id=completed.user_event_id,
    )
    return _web_redirect(
        settings,
        connected=completed.definition.key,
        account=completed.account.email,
    )


@webhook_router.post("/calendar", status_code=status.HTTP_204_NO_CONTENT)
async def google_calendar_webhook(
    session: Annotated[AsyncSession, Depends(get_session)],
    channel_id: Annotated[str | None, Header(alias="X-Goog-Channel-ID")] = None,
    resource_id: Annotated[str | None, Header(alias="X-Goog-Resource-ID")] = None,
    resource_state: Annotated[str | None, Header(alias="X-Goog-Resource-State")] = None,
    message_number: Annotated[str | None, Header(alias="X-Goog-Message-Number")] = None,
    channel_token: Annotated[str | None, Header(alias="X-Goog-Channel-Token")] = None,
) -> Response:
    if not channel_id or not resource_id or not message_number or not channel_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing headers")
    subscription = await session.scalar(
        select(IntegrationSubscription).where(
            IntegrationSubscription.provider == "google",
            IntegrationSubscription.integration_key == "google_calendar",
            IntegrationSubscription.provider_subscription_id == channel_id,
        )
    )
    if (
        subscription is None
        or (
            subscription.provider_resource_id is not None
            and subscription.provider_resource_id != resource_id
        )
        or subscription.verification_token_hash != _token_hash(channel_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Calendar notification",
        )
    external_event_id = f"{channel_id}:{message_number}"
    existing = await session.scalar(
        select(WebhookEvent.id).where(
            WebhookEvent.provider == "google_calendar",
            WebhookEvent.external_event_id == external_event_id,
        )
    )
    if existing is None:
        now = datetime.now(UTC)
        if subscription.provider_resource_id is None:
            subscription.provider_resource_id = resource_id
        session.add(
            WebhookEvent(
                provider="google_calendar",
                external_event_id=external_event_id,
                event_type=resource_state or "exists",
                status=WebhookStatus.PROCESSED.value,
                payload={
                    "integration_account_id": str(subscription.account_id),
                    "subscription_id": str(subscription.id),
                    "resource_id": resource_id,
                    "message_number": message_number,
                },
                processed_at=now,
            )
        )
        subscription.last_notification_at = now
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@webhook_router.post("/gmail", status_code=status.HTTP_204_NO_CONTENT)
async def google_gmail_webhook(
    payload: dict[str, Any],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: str | None = None,
) -> Response:
    expected_token = settings.google_pubsub_push_verification_token
    if expected_token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail webhook verification is not configured",
        )
    if token is None or not secrets.compare_digest(token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    message = payload.get("message")
    if not isinstance(message, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing message")
    message_id = message.get("messageId")
    encoded_data = message.get("data")
    if not isinstance(message_id, str) or not isinstance(encoded_data, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Pub/Sub message",
        )
    data = _decode_pubsub_data(encoded_data)
    email = data.get("emailAddress")
    history_id = data.get("historyId")
    if not isinstance(email, str) or history_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Gmail notification",
        )
    account = await session.scalar(
        select(IntegrationAccount).where(
            IntegrationAccount.provider == "google",
            IntegrationAccount.email == email.lower(),
        )
    )
    if account is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    subscription = await session.scalar(
        select(IntegrationSubscription).where(
            IntegrationSubscription.account_id == account.id,
            IntegrationSubscription.integration_key == "gmail",
        )
    )
    if subscription is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    existing = await session.scalar(
        select(WebhookEvent.id).where(
            WebhookEvent.provider == "google_gmail",
            WebhookEvent.external_event_id == message_id,
        )
    )
    if existing is None:
        now = datetime.now(UTC)
        session.add(
            WebhookEvent(
                provider="google_gmail",
                external_event_id=message_id,
                event_type="history.updated",
                status=WebhookStatus.PROCESSED.value,
                payload={
                    "integration_account_id": str(account.id),
                    "subscription_id": str(subscription.id),
                    "email_address": email.lower(),
                    "history_id": str(history_id),
                },
                processed_at=now,
            )
        )
        subscription.cursor = str(history_id)
        subscription.last_notification_at = now
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@plaid_webhook_router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def plaid_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    plaid_verification: Annotated[str | None, Header(alias="Plaid-Verification")] = None,
) -> Response:
    if plaid_verification is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Plaid verification signature",
        )
    try:
        client = build_plaid_client(settings)
    except FinancialIntegrationNotConfiguredError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    raw_body = await request.body()
    if not await client.verify_webhook(body=raw_body, signed_jwt=plaid_verification):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Plaid webhook signature",
        )
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Plaid webhook payload",
        ) from error
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Plaid webhook payload",
        )
    item_id = payload.get("item_id")
    webhook_type = payload.get("webhook_type")
    webhook_code = payload.get("webhook_code")
    if not all(isinstance(value, str) for value in (item_id, webhook_type, webhook_code)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Plaid webhook is missing required fields",
        )
    connection = await session.scalar(
        select(FinancialConnection).where(
            FinancialConnection.provider == "plaid",
            FinancialConnection.provider_connection_id == item_id,
        )
    )
    if connection is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    cursor_key = hashlib.sha256(
        f"{connection.sync_cursor or 'initial'}:{webhook_type}:{webhook_code}".encode()
    ).hexdigest()
    external_event_id = f"{item_id}:{cursor_key}"
    existing = await session.scalar(
        select(WebhookEvent).where(
            WebhookEvent.provider == "plaid",
            WebhookEvent.external_event_id == external_event_id,
        )
    )
    if existing is not None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    now = datetime.now(UTC)
    event = WebhookEvent(
        provider="plaid",
        external_event_id=external_event_id,
        event_type=f"{webhook_type}.{webhook_code}",
        status=WebhookStatus.PROCESSED.value,
        payload=payload,
        processed_at=now,
    )
    session.add(event)
    if webhook_code == "SYNC_UPDATES_AVAILABLE":
        await enqueue_plaid_sync(
            session,
            connection=connection,
            reason=webhook_code,
            idempotency_suffix=cursor_key,
        )
    error_payload = payload.get("error")
    if isinstance(error_payload, dict) and error_payload.get("error_code") in {
        "ITEM_LOGIN_REQUIRED",
        "PENDING_DISCONNECT",
    }:
        connection.status = FinancialConnectionStatus.NEEDS_REAUTHORIZATION.value
        await enqueue_user_event(
            session,
            user_id=connection.user_id,
            event_type="finance.reauth_required",
            source="plaid_webhook",
            idempotency_key=f"finance.reauth:{connection.id}:{cursor_key}",
            payload={
                "connection_id": str(connection.id),
                "institution_name": connection.institution_name,
                "reconnect_url": f"{settings.web_app_url}/?tab=integrations",
            },
            delivery_provider="linq",
        )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _web_redirect(settings: Settings, **query_values: str) -> RedirectResponse:
    query = urlencode({"tab": "integrations", **query_values})
    return RedirectResponse(
        f"{settings.web_app_url}/?{query}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _decode_pubsub_data(encoded_data: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(encoded_data) % 4)
        decoded = base64.urlsafe_b64decode(encoded_data + padding)
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Pub/Sub data",
        ) from error
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Pub/Sub data",
        )
    return payload
