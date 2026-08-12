import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from benji_api.config import Settings
from benji_api.db.session import async_session_factory
from benji_api.integrations.google.client import GoogleIntegrationClient
from benji_api.integrations.linq.client import LinqClient
from benji_api.integrations.plaid.client import PlaidClient
from benji_api.models.channel import (
    Conversation,
    ConversationKind,
    ConversationMember,
    ConversationMemberStatus,
    Message,
    MessageAttachment,
)
from benji_api.models.finance import FinancialConnection
from benji_api.models.integration import IntegrationAccount, IntegrationGrant, IntegrationStatus
from benji_api.models.schedule import ScheduledTask, ScheduledTaskStatus
from benji_api.models.user import UserIdentifier
from benji_api.services.finance import disconnect_financial_connection
from benji_api.services.groups import transfer_group_ownership
from benji_api.services.integrations import disconnect_google_integration
from benji_api.services.schedules import cancel_scheduled_task, create_scheduled_task
from benji_api.services.user_reset import build_user_reset_plan, execute_user_reset

logger = logging.getLogger(__name__)

ACCOUNT_DELETION_ACTION = "account.delete"
ACCOUNT_DELETION_GRACE_SECONDS = 60


async def schedule_account_deletion(
    session: AsyncSession,
    *,
    user_id: UUID,
    conversation_id: UUID,
) -> ScheduledTask:
    existing = await session.scalar(
        select(ScheduledTask)
        .where(
            ScheduledTask.user_id == user_id,
            ScheduledTask.action_type == ACCOUNT_DELETION_ACTION,
            ScheduledTask.status.in_(
                (
                    ScheduledTaskStatus.ACTIVE.value,
                    ScheduledTaskStatus.PROCESSING.value,
                    ScheduledTaskStatus.FAILED.value,
                )
            ),
        )
        .order_by(ScheduledTask.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        return existing
    now = datetime.now(UTC)
    task = await create_scheduled_task(
        session,
        user_id=user_id,
        conversation_id=conversation_id,
        action_type=ACCOUNT_DELETION_ACTION,
        source="agent",
        idempotency_key=f"account.delete:{user_id}:{uuid4()}",
        title="Delete Dot account",
        payload={},
        run_at=now + timedelta(seconds=ACCOUNT_DELETION_GRACE_SECONDS),
    )
    await session.flush()
    return task


async def cancel_account_deletion(session: AsyncSession, *, user_id: UUID) -> bool:
    task = await session.scalar(
        select(ScheduledTask)
        .where(
            ScheduledTask.user_id == user_id,
            ScheduledTask.action_type == ACCOUNT_DELETION_ACTION,
            ScheduledTask.status.in_(
                (
                    ScheduledTaskStatus.ACTIVE.value,
                    ScheduledTaskStatus.FAILED.value,
                )
            ),
        )
        .order_by(ScheduledTask.created_at.desc())
        .limit(1)
    )
    if task is None:
        return False
    return await cancel_scheduled_task(session, user_id=user_id, task_id=task.id)


async def execute_account_deletion(
    *,
    user_id: UUID,
    settings: Settings,
    google_client: GoogleIntegrationClient | None = None,
    plaid_client: PlaidClient | None = None,
    linq_client: LinqClient | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Best-effort provider revocation followed by a complete local account purge."""
    factory = session_factory or async_session_factory
    await _disconnect_google_accounts(
        factory,
        user_id=user_id,
        settings=settings,
        google_client=google_client,
    )
    await _disconnect_financial_accounts(
        factory,
        user_id=user_id,
        settings=settings,
        plaid_client=plaid_client,
    )
    async with factory() as session:
        await _transfer_owned_groups(session, user_id=user_id)
        await _delete_private_message_attachments(
            session,
            user_id=user_id,
            settings=settings,
            linq_client=linq_client,
        )
        identifier = await session.scalar(
            select(UserIdentifier)
            .where(UserIdentifier.user_id == user_id)
            .order_by(UserIdentifier.is_primary.desc(), UserIdentifier.created_at)
            .limit(1)
        )
        if identifier is None:
            raise RuntimeError("Account has no canonical identifier")
        plan = await build_user_reset_plan(session, identifier.normalized_value)
        if plan.user_id != user_id:
            raise RuntimeError("Account identity changed before deletion")
        await execute_user_reset(session, plan)
        await session.commit()


async def _disconnect_google_accounts(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    settings: Settings,
    google_client: GoogleIntegrationClient | None,
) -> None:
    async with factory() as session:
        grants = list(
            (
                await session.execute(
                    select(IntegrationAccount.id, IntegrationGrant.integration_key)
                    .join(IntegrationGrant, IntegrationGrant.account_id == IntegrationAccount.id)
                    .where(
                        IntegrationAccount.user_id == user_id,
                        IntegrationAccount.provider == "google",
                        IntegrationGrant.status != IntegrationStatus.REVOKED.value,
                    )
                    .order_by(IntegrationAccount.id, IntegrationGrant.integration_key)
                )
            ).all()
        )
    for account_id, integration_key in grants:
        try:
            async with factory() as session:
                await disconnect_google_integration(
                    session,
                    user_id=user_id,
                    account_id=account_id,
                    integration_key=integration_key,
                    settings=settings,
                    google_client=google_client,
                )
                await session.commit()
        except Exception:
            # Losing the encrypted local credential still prevents Dot from accessing the data.
            # Do not hold the user's local deletion hostage to an unavailable third party.
            logger.exception(
                "Could not revoke Google integration %s for deleted user %s",
                integration_key,
                user_id,
            )


async def _disconnect_financial_accounts(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    settings: Settings,
    plaid_client: PlaidClient | None,
) -> None:
    async with factory() as session:
        connection_ids = tuple(
            (
                await session.scalars(
                    select(FinancialConnection.id).where(FinancialConnection.user_id == user_id)
                )
            ).all()
        )
    for connection_id in connection_ids:
        try:
            async with factory() as session:
                await disconnect_financial_connection(
                    session,
                    user_id=user_id,
                    connection_id=connection_id,
                    settings=settings,
                    plaid_client=plaid_client,
                )
                await session.commit()
        except Exception:
            logger.exception(
                "Could not revoke financial connection %s for deleted user %s",
                connection_id,
                user_id,
            )


async def _transfer_owned_groups(session: AsyncSession, *, user_id: UUID) -> None:
    groups = list(
        (
            await session.scalars(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.kind == ConversationKind.GROUP.value,
                )
            )
        ).all()
    )
    for conversation in groups:
        replacement = await session.scalar(
            select(ConversationMember)
            .where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.user_id.is_not(None),
                ConversationMember.user_id != user_id,
                ConversationMember.status == ConversationMemberStatus.ACTIVE.value,
            )
            .order_by(ConversationMember.joined_at, ConversationMember.created_at)
            .limit(1)
        )
        if replacement is None or replacement.user_id is None:
            continue
        await transfer_group_ownership(
            session,
            conversation=conversation,
            successor=replacement,
            source="transferred",
            departing_owner_user_id=user_id,
        )
        await session.execute(
            update(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.user_id == user_id,
            )
            .values(user_id=replacement.user_id)
        )
    await session.flush()


async def _delete_private_message_attachments(
    session: AsyncSession,
    *,
    user_id: UUID,
    settings: Settings,
    linq_client: LinqClient | None,
) -> None:
    attachment_rows = list(
        (
            await session.execute(
                select(
                    MessageAttachment.id,
                    MessageAttachment.provider,
                    MessageAttachment.provider_attachment_id,
                )
                .join(Message, Message.id == MessageAttachment.message_id)
                .where(Message.user_id == user_id)
            )
        ).all()
    )
    provider_ids = {
        provider_attachment_id
        for _, provider, provider_attachment_id in attachment_rows
        if provider == "linq" and provider_attachment_id
    }
    client = linq_client
    if client is None and settings.linq_api_key:
        client = LinqClient(
            api_key=settings.linq_api_key,
            base_url=settings.linq_api_base_url,
            timeout_seconds=settings.linq_request_timeout_seconds,
        )
    if client is not None:
        for attachment_id in sorted(provider_ids):
            try:
                await client.delete_attachment(attachment_id=attachment_id)
            except Exception:
                logger.exception(
                    "Could not delete Linq attachment %s for deleted user %s",
                    attachment_id,
                    user_id,
                )
    attachment_ids = [attachment_id for attachment_id, _, _ in attachment_rows]
    if attachment_ids:
        await session.execute(
            delete(MessageAttachment).where(MessageAttachment.id.in_(attachment_ids))
        )
