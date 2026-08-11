from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from benji_api.config import Settings
from benji_api.db.session import async_session_factory
from benji_api.integrations.plaid.client import PlaidClient, PlaidProviderError
from benji_api.models.finance import (
    FinancialAccount,
    FinancialConnection,
    FinancialConnectionStatus,
    FinancialLinkSession,
    FinancialSyncStatus,
    FinancialTransaction,
)
from benji_api.services.integration_credentials import IntegrationCredentialVault
from benji_api.services.schedules import (
    FINANCIAL_SYNC_ACTION,
    cancel_scheduled_task,
    create_scheduled_task,
    list_scheduled_tasks,
)
from benji_api.services.user_events import enqueue_user_event


class FinancialIntegrationNotConfiguredError(RuntimeError):
    pass


class FinancialAuthorizationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FinancialLinkToken:
    link_token: str
    exchange_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CompletedFinancialConnection:
    connection: FinancialConnection
    sync_task_id: UUID


def build_plaid_client(settings: Settings) -> PlaidClient:
    if settings.plaid_client_id is None or settings.plaid_secret is None:
        raise FinancialIntegrationNotConfiguredError("Plaid is not configured")
    environment_urls = {
        "sandbox": "https://sandbox.plaid.com",
        "development": "https://development.plaid.com",
        "production": "https://production.plaid.com",
    }
    base_url = environment_urls.get(settings.plaid_environment)
    if base_url is None:
        raise FinancialIntegrationNotConfiguredError(
            "PLAID_ENV must be sandbox, development, or production"
        )
    return PlaidClient(
        client_id=settings.plaid_client_id,
        secret=settings.plaid_secret,
        base_url=base_url,
        timeout_seconds=settings.plaid_request_timeout_seconds,
    )


async def create_plaid_link_token(
    session: AsyncSession,
    *,
    user_id: UUID,
    initiated_channel: str,
    settings: Settings,
    plaid_client: PlaidClient | None = None,
    connection_id: UUID | None = None,
    commit: bool = True,
) -> FinancialLinkToken:
    if settings.integration_token_encryption_key is None:
        raise FinancialIntegrationNotConfiguredError(
            "Integration credential encryption is not configured"
        )
    client = plaid_client or build_plaid_client(settings)
    access_token = None
    if connection_id is not None:
        connection = await session.scalar(
            select(FinancialConnection).where(
                FinancialConnection.id == connection_id,
                FinancialConnection.user_id == user_id,
                FinancialConnection.provider == "plaid",
            )
        )
        if connection is None:
            raise FinancialAuthorizationError("Financial connection was not found")
        vault = IntegrationCredentialVault(settings.integration_token_encryption_key)
        credentials = vault.decrypt(connection.credentials_ciphertext)
        candidate = credentials.get("access_token")
        if not isinstance(candidate, str):
            raise FinancialAuthorizationError("Financial connection credentials are invalid")
        access_token = candidate
    result = await client.create_link_token(
        client_user_id=str(user_id),
        client_name="Dot",
        country_codes=settings.plaid_country_codes,
        webhook_url=settings.plaid_webhook_url,
        redirect_uri=settings.plaid_redirect_uri,
        access_token=access_token,
    )
    link_token = result.get("link_token")
    expiration = result.get("expiration")
    if not isinstance(link_token, str) or not isinstance(expiration, str):
        raise PlaidProviderError("Plaid did not return a valid Link token")
    expires_at = _parse_datetime(expiration)
    raw_exchange_token = secrets.token_urlsafe(32)
    session.add(
        FinancialLinkSession(
            user_id=user_id,
            connection_id=connection_id,
            provider="plaid",
            exchange_token_hash=_token_hash(raw_exchange_token),
            initiated_channel=initiated_channel,
            expires_at=expires_at,
        )
    )
    if commit:
        await session.commit()
    else:
        await session.flush()
    return FinancialLinkToken(
        link_token=link_token,
        exchange_token=raw_exchange_token,
        expires_at=expires_at,
    )


async def complete_plaid_link(
    session: AsyncSession,
    *,
    exchange_token: str,
    public_token: str,
    institution_id: str | None,
    institution_name: str | None,
    settings: Settings,
    plaid_client: PlaidClient | None = None,
) -> CompletedFinancialConnection:
    link_session = await session.scalar(
        select(FinancialLinkSession).where(
            FinancialLinkSession.exchange_token_hash == _token_hash(exchange_token)
        )
    )
    now = datetime.now(UTC)
    if (
        link_session is None
        or link_session.provider != "plaid"
        or link_session.consumed_at is not None
        or _as_utc(link_session.expires_at) <= now
    ):
        raise FinancialAuthorizationError("This bank connection session is invalid or expired")
    if settings.integration_token_encryption_key is None:
        raise FinancialIntegrationNotConfiguredError(
            "Integration credential encryption is not configured"
        )
    client = plaid_client or build_plaid_client(settings)
    existing_connection = None
    if link_session.connection_id is not None:
        existing_connection = await session.scalar(
            select(FinancialConnection).where(
                FinancialConnection.id == link_session.connection_id,
                FinancialConnection.user_id == link_session.user_id,
                FinancialConnection.provider == "plaid",
            )
        )
        if existing_connection is None:
            raise FinancialAuthorizationError("Financial connection was not found")
        item_id = existing_connection.provider_connection_id
        vault = IntegrationCredentialVault(settings.integration_token_encryption_key)
        stored_credentials = vault.decrypt(existing_connection.credentials_ciphertext)
        access_token = stored_credentials.get("access_token")
        if not isinstance(access_token, str):
            raise FinancialAuthorizationError("Financial connection credentials are invalid")
    else:
        exchanged = await client.exchange_public_token(public_token)
        access_token = exchanged.get("access_token")
        item_id = exchanged.get("item_id")
        if not isinstance(access_token, str) or not isinstance(item_id, str):
            raise PlaidProviderError("Plaid did not return a valid financial connection")

    connection = existing_connection or await session.scalar(
        select(FinancialConnection).where(
            FinancialConnection.provider == "plaid",
            FinancialConnection.provider_connection_id == item_id,
        )
    )
    if connection is not None and connection.user_id != link_session.user_id:
        raise FinancialAuthorizationError(
            "This financial institution is already connected to another Dot user"
        )
    vault = IntegrationCredentialVault(settings.integration_token_encryption_key)
    credentials = vault.encrypt({"access_token": access_token})
    if connection is None:
        connection = FinancialConnection(
            user_id=link_session.user_id,
            provider="plaid",
            provider_connection_id=item_id,
            institution_id=institution_id,
            institution_name=(institution_name or "Connected institution")[:255],
            credentials_ciphertext=credentials,
        )
        session.add(connection)
        await session.flush()
    else:
        connection.institution_id = institution_id or connection.institution_id
        connection.institution_name = (institution_name or connection.institution_name)[:255]
        connection.credentials_ciphertext = credentials
        connection.status = FinancialConnectionStatus.ACTIVE.value
        connection.sync_status = FinancialSyncStatus.PENDING.value
        connection.last_sync_error = None

    link_session.consumed_at = now
    delivery_provider = "linq" if link_session.initiated_channel == "messaging_link" else None
    sync_task = await create_scheduled_task(
        session,
        user_id=link_session.user_id,
        conversation_id=None,
        action_type=FINANCIAL_SYNC_ACTION,
        source="plaid_link",
        idempotency_key=f"plaid.sync:{connection.id}:link:{link_session.id}",
        title=f"Initial sync for {connection.institution_name}",
        payload={"connection_id": str(connection.id), "notify_on_complete": True},
        run_at=now,
        delivery_provider=delivery_provider,
    )
    await session.commit()
    return CompletedFinancialConnection(connection=connection, sync_task_id=sync_task.id)


async def enqueue_plaid_sync(
    session: AsyncSession,
    *,
    connection: FinancialConnection,
    reason: str,
    idempotency_suffix: str,
) -> UUID:
    task = await create_scheduled_task(
        session,
        user_id=connection.user_id,
        conversation_id=None,
        action_type=FINANCIAL_SYNC_ACTION,
        source="plaid_webhook",
        idempotency_key=f"plaid.sync:{connection.id}:{idempotency_suffix}",
        title=f"Sync {connection.institution_name}",
        payload={"connection_id": str(connection.id), "reason": reason},
        run_at=datetime.now(UTC),
    )
    await session.flush()
    return task.id


async def disconnect_financial_connection(
    session: AsyncSession,
    *,
    user_id: UUID,
    connection_id: UUID,
    settings: Settings,
    plaid_client: PlaidClient | None = None,
) -> bool:
    connection = await session.scalar(
        select(FinancialConnection).where(
            FinancialConnection.id == connection_id,
            FinancialConnection.user_id == user_id,
        )
    )
    if connection is None:
        return False
    if connection.provider != "plaid":
        raise RuntimeError("Financial provider is not supported for disconnection")
    if settings.integration_token_encryption_key is None:
        raise FinancialIntegrationNotConfiguredError(
            "Integration credential encryption is not configured"
        )
    vault = IntegrationCredentialVault(settings.integration_token_encryption_key)
    credentials = vault.decrypt(connection.credentials_ciphertext)
    access_token = credentials.get("access_token")
    if not isinstance(access_token, str):
        raise FinancialAuthorizationError("Financial connection credentials are invalid")
    client = plaid_client or build_plaid_client(settings)
    await client.remove_item(access_token)
    tasks = await list_scheduled_tasks(
        session,
        user_id=user_id,
        action_type=FINANCIAL_SYNC_ACTION,
    )
    for task in tasks:
        if task.payload.get("connection_id") == str(connection_id):
            await cancel_scheduled_task(session, user_id=user_id, task_id=task.id)
    account_ids = tuple(
        (
            await session.scalars(
                select(FinancialAccount.id).where(
                    FinancialAccount.connection_id == connection_id
                )
            )
        ).all()
    )
    if account_ids:
        await session.execute(
            delete(FinancialTransaction).where(
                FinancialTransaction.account_id.in_(account_ids)
            )
        )
        await session.execute(
            delete(FinancialAccount).where(FinancialAccount.id.in_(account_ids))
        )
    await session.execute(
        delete(FinancialLinkSession).where(
            FinancialLinkSession.connection_id == connection_id
        )
    )
    await session.delete(connection)
    await session.flush()
    return True


async def sync_financial_connection(
    *,
    connection_id: UUID,
    settings: Settings,
    notify_on_complete: bool = False,
    delivery_provider: str | None = None,
    plaid_client: PlaidClient | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    factory = session_factory or async_session_factory
    client = plaid_client or build_plaid_client(settings)
    if settings.integration_token_encryption_key is None:
        raise FinancialIntegrationNotConfiguredError(
            "Integration credential encryption is not configured"
        )
    vault = IntegrationCredentialVault(settings.integration_token_encryption_key)
    async with factory() as session:
        connection = await session.get(FinancialConnection, connection_id)
        if connection is None or connection.provider != "plaid":
            raise RuntimeError("Financial connection was not found")
        credentials = vault.decrypt(connection.credentials_ciphertext)
        access_token = credentials.get("access_token")
        if not isinstance(access_token, str):
            raise RuntimeError("Financial connection credentials are invalid")
        original_cursor = connection.sync_cursor
        connection.sync_status = FinancialSyncStatus.SYNCING.value
        connection.last_sync_error = None
        await session.commit()

    try:
        pages = await _load_transaction_pages(
            client,
            access_token=access_token,
            original_cursor=original_cursor,
        )
        async with factory() as session:
            connection = await session.get(FinancialConnection, connection_id)
            if connection is None:
                raise RuntimeError("Financial connection was removed during sync")
            account_map = await _upsert_accounts(session, connection=connection, pages=pages)
            for page in pages:
                for transaction in page.get("added", []):
                    await _upsert_transaction(
                        session,
                        connection=connection,
                        account_map=account_map,
                        raw=transaction,
                    )
                for transaction in page.get("modified", []):
                    await _upsert_transaction(
                        session,
                        connection=connection,
                        account_map=account_map,
                        raw=transaction,
                    )
                for removed in page.get("removed", []):
                    provider_id = removed.get("transaction_id")
                    if not isinstance(provider_id, str):
                        continue
                    stored = await session.scalar(
                        select(FinancialTransaction).where(
                            FinancialTransaction.source == "plaid",
                            FinancialTransaction.provider_transaction_id == provider_id,
                        )
                    )
                    if stored is not None:
                        stored.removed_at = datetime.now(UTC)
            final_cursor = pages[-1].get("next_cursor") if pages else original_cursor
            if isinstance(final_cursor, str):
                connection.sync_cursor = final_cursor
            connection.sync_status = FinancialSyncStatus.IDLE.value
            connection.status = FinancialConnectionStatus.ACTIVE.value
            connection.last_synced_at = datetime.now(UTC)
            connection.last_sync_error = None
            if notify_on_complete:
                await enqueue_user_event(
                    session,
                    user_id=connection.user_id,
                    event_type="finance.connected",
                    source="plaid_sync",
                    idempotency_key=f"finance.connected:{connection.id}",
                    payload={
                        "provider": "plaid",
                        "connection_id": str(connection.id),
                        "institution_name": connection.institution_name,
                        "account_count": len(account_map),
                    },
                    delivery_provider=delivery_provider,
                )
            await session.commit()
    except Exception as error:
        async with factory() as session:
            connection = await session.get(FinancialConnection, connection_id)
            if connection is not None:
                connection.sync_status = FinancialSyncStatus.FAILED.value
                connection.last_sync_error = str(error)[:2_000]
                if isinstance(error, PlaidProviderError) and error.code in {
                    "ITEM_LOGIN_REQUIRED",
                    "PENDING_DISCONNECT",
                }:
                    connection.status = FinancialConnectionStatus.NEEDS_REAUTHORIZATION.value
                await session.commit()
        raise


async def _load_transaction_pages(
    client: PlaidClient,
    *,
    access_token: str,
    original_cursor: str | None,
) -> list[dict[str, Any]]:
    for mutation_attempt in range(3):
        cursor = original_cursor
        pages: list[dict[str, Any]] = []
        try:
            while True:
                page = await client.sync_transactions(
                    access_token=access_token,
                    cursor=cursor,
                )
                pages.append(page)
                next_cursor = page.get("next_cursor")
                if isinstance(next_cursor, str):
                    cursor = next_cursor
                if page.get("has_more") is not True:
                    return pages
        except PlaidProviderError as error:
            if (
                error.code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
                and mutation_attempt < 2
            ):
                continue
            raise
    raise RuntimeError("Financial transaction sync could not stabilize")


async def _upsert_accounts(
    session: AsyncSession,
    *,
    connection: FinancialConnection,
    pages: list[dict[str, Any]],
) -> dict[str, FinancialAccount]:
    raw_accounts: dict[str, dict[str, Any]] = {}
    for page in pages:
        for raw in page.get("accounts", []):
            provider_id = raw.get("account_id") if isinstance(raw, dict) else None
            if isinstance(provider_id, str):
                raw_accounts[provider_id] = raw
    account_map: dict[str, FinancialAccount] = {}
    for provider_id, raw in raw_accounts.items():
        account = await session.scalar(
            select(FinancialAccount).where(
                FinancialAccount.connection_id == connection.id,
                FinancialAccount.provider_account_id == provider_id,
            )
        )
        balances = raw.get("balances") if isinstance(raw.get("balances"), dict) else {}
        if account is None:
            account = FinancialAccount(
                connection_id=connection.id,
                provider_account_id=provider_id,
                name=str(raw.get("name") or "Account")[:255],
                account_type=str(raw.get("type") or "other")[:64],
            )
            session.add(account)
            await session.flush()
        account.name = str(raw.get("name") or account.name)[:255]
        official_name = raw.get("official_name")
        account.official_name = str(official_name)[:255] if official_name is not None else None
        mask = raw.get("mask")
        account.mask = str(mask)[:16] if mask is not None else None
        account.account_type = str(raw.get("type") or account.account_type)[:64]
        subtype = raw.get("subtype")
        account.account_subtype = str(subtype)[:64] if subtype is not None else None
        currency = balances.get("iso_currency_code") or balances.get("unofficial_currency_code")
        account.currency = str(currency)[:16] if currency is not None else None
        account.current_balance = _optional_decimal(balances.get("current"))
        account.available_balance = _optional_decimal(balances.get("available"))
        account.metadata_json = {"persistent_account_id": raw.get("persistent_account_id")}
        account_map[provider_id] = account
    return account_map


async def _upsert_transaction(
    session: AsyncSession,
    *,
    connection: FinancialConnection,
    account_map: dict[str, FinancialAccount],
    raw: Any,
) -> None:
    if not isinstance(raw, dict):
        return
    provider_id = raw.get("transaction_id")
    provider_account_id = raw.get("account_id")
    if not isinstance(provider_id, str) or not isinstance(provider_account_id, str):
        return
    account = account_map.get(provider_account_id)
    if account is None:
        return
    transaction = await session.scalar(
        select(FinancialTransaction).where(
            FinancialTransaction.source == "plaid",
            FinancialTransaction.provider_transaction_id == provider_id,
        )
    )
    transaction_date = _parse_date(raw.get("date"))
    if transaction_date is None:
        return
    amount = _optional_decimal(raw.get("amount"))
    if amount is None:
        return
    if transaction is None:
        transaction = FinancialTransaction(
            user_id=connection.user_id,
            account_id=account.id,
            source="plaid",
            provider_transaction_id=provider_id,
            amount=amount,
            transaction_date=transaction_date,
            name=str(raw.get("name") or "Transaction")[:500],
        )
        session.add(transaction)
    transaction.account_id = account.id
    transaction.amount = amount
    transaction.currency = _optional_string(
        raw.get("iso_currency_code") or raw.get("unofficial_currency_code"), 16
    )
    transaction.transaction_date = transaction_date
    transaction.authorized_date = _parse_date(raw.get("authorized_date"))
    transaction.name = str(raw.get("name") or "Transaction")[:500]
    transaction.merchant_name = _optional_string(raw.get("merchant_name"), 255)
    transaction.pending = raw.get("pending") is True
    transaction.pending_transaction_id = _optional_string(raw.get("pending_transaction_id"), 255)
    category = raw.get("personal_finance_category")
    transaction.category_json = category if isinstance(category, dict) else {}
    transaction.metadata_json = {
        "logo_url": raw.get("logo_url"),
        "website": raw.get("website"),
        "payment_channel": raw.get("payment_channel"),
        "location": raw.get("location") if isinstance(raw.get("location"), dict) else {},
    }
    transaction.removed_at = None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise PlaidProviderError("Plaid returned an invalid expiration time")
    return parsed.astimezone(UTC)


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _optional_string(value: Any, max_length: int) -> str | None:
    return str(value)[:max_length] if value is not None else None


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
