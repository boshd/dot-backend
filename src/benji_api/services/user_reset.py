from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.models import (
    AgentRun,
    AgentToolCall,
    AuthIdentity,
    Conversation,
    ConversationChannel,
    ConversationInvite,
    ConversationMember,
    FinancialAccount,
    FinancialConnection,
    FinancialGoal,
    FinancialLinkSession,
    FinancialTransaction,
    GeneratedApp,
    GeneratedAppAccessTicket,
    GeneratedAppBuildJob,
    GeneratedAppDataRecord,
    GeneratedAppDeployment,
    GeneratedAppEvent,
    GeneratedAppMembership,
    GeneratedAppRecord,
    GeneratedAppRevision,
    GeneratedAppSession,
    GeneratedAppVersion,
    IntegrationAccount,
    IntegrationConnectLink,
    IntegrationGrant,
    IntegrationOAuthState,
    IntegrationSubscription,
    MemoryEntity,
    MemoryEpisode,
    MemoryEvidence,
    MemoryFact,
    MemoryJob,
    Message,
    MessageDelivery,
    ScheduledTask,
    User,
    UserEvent,
    UserIdentifier,
    WebhookEvent,
)
from benji_api.services.users import normalize_email_address, normalize_user_identifier


@dataclass(frozen=True, slots=True)
class UserResetPlan:
    normalized_identifier: str
    identifier_kind: str
    user_id: UUID | None
    user_identifier_ids: tuple[UUID, ...]
    conversation_ids: tuple[UUID, ...]
    conversation_member_ids: tuple[UUID, ...]
    conversation_invite_ids: tuple[UUID, ...]
    generated_app_ids: tuple[UUID, ...]
    generated_app_version_ids: tuple[UUID, ...]
    generated_app_record_ids: tuple[UUID, ...]
    generated_app_revision_ids: tuple[UUID, ...]
    generated_app_build_job_ids: tuple[UUID, ...]
    generated_app_membership_ids: tuple[UUID, ...]
    generated_app_data_record_ids: tuple[UUID, ...]
    generated_app_event_ids: tuple[UUID, ...]
    generated_app_access_ticket_ids: tuple[UUID, ...]
    generated_app_session_ids: tuple[UUID, ...]
    auth_identity_ids: tuple[UUID, ...]
    integration_account_ids: tuple[UUID, ...]
    integration_grant_ids: tuple[UUID, ...]
    integration_oauth_state_ids: tuple[UUID, ...]
    integration_connect_link_ids: tuple[UUID, ...]
    integration_subscription_ids: tuple[UUID, ...]
    financial_connection_ids: tuple[UUID, ...]
    financial_link_session_ids: tuple[UUID, ...]
    financial_account_ids: tuple[UUID, ...]
    financial_transaction_ids: tuple[UUID, ...]
    financial_goal_ids: tuple[UUID, ...]
    scheduled_task_ids: tuple[UUID, ...]
    channel_ids: tuple[UUID, ...]
    message_ids: tuple[UUID, ...]
    delivery_ids: tuple[UUID, ...]
    agent_run_ids: tuple[UUID, ...]
    tool_call_ids: tuple[UUID, ...]
    user_event_ids: tuple[UUID, ...]
    memory_job_ids: tuple[UUID, ...]
    memory_episode_ids: tuple[UUID, ...]
    memory_entity_ids: tuple[UUID, ...]
    memory_fact_ids: tuple[UUID, ...]
    memory_evidence_ids: tuple[UUID, ...]
    webhook_event_ids: tuple[UUID, ...]

    @property
    def total_records(self) -> int:
        return sum(
            (
                self.user_id is not None,
                len(self.user_identifier_ids),
                len(self.conversation_ids),
                len(self.conversation_member_ids),
                len(self.conversation_invite_ids),
                len(self.generated_app_ids),
                len(self.generated_app_version_ids),
                len(self.generated_app_record_ids),
                len(self.generated_app_revision_ids),
                len(self.generated_app_build_job_ids),
                len(self.generated_app_membership_ids),
                len(self.generated_app_data_record_ids),
                len(self.generated_app_event_ids),
                len(self.generated_app_access_ticket_ids),
                len(self.generated_app_session_ids),
                len(self.auth_identity_ids),
                len(self.integration_account_ids),
                len(self.integration_grant_ids),
                len(self.integration_oauth_state_ids),
                len(self.integration_connect_link_ids),
                len(self.integration_subscription_ids),
                len(self.financial_connection_ids),
                len(self.financial_link_session_ids),
                len(self.financial_account_ids),
                len(self.financial_transaction_ids),
                len(self.financial_goal_ids),
                len(self.scheduled_task_ids),
                len(self.channel_ids),
                len(self.message_ids),
                len(self.delivery_ids),
                len(self.agent_run_ids),
                len(self.tool_call_ids),
                len(self.user_event_ids),
                len(self.memory_job_ids),
                len(self.memory_episode_ids),
                len(self.memory_entity_ids),
                len(self.memory_fact_ids),
                len(self.memory_evidence_ids),
                len(self.webhook_event_ids),
            )
        )


def _contains_exact_value(value: Any, targets: set[str]) -> bool:
    if isinstance(value, dict):
        return any(_contains_exact_value(item, targets) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact_value(item, targets) for item in value)
    if not isinstance(value, str):
        return False
    if value in targets:
        return True
    if "@" not in value:
        return False
    try:
        return normalize_email_address(value) in targets
    except ValueError:
        return False


async def build_user_reset_plan(
    session: AsyncSession,
    identifier_value: str,
) -> UserResetPlan:
    normalized = normalize_user_identifier(identifier_value)
    identifier = await session.scalar(
        select(UserIdentifier).where(
            UserIdentifier.kind == normalized.kind.value,
            UserIdentifier.normalized_value == normalized.value,
        )
    )
    user_id = identifier.user_id if identifier is not None else None
    if user_id is None and normalized.kind.value == "phone":
        user_id = await session.scalar(select(User.id).where(User.phone_number == normalized.value))

    user_identifier_ids: tuple[UUID, ...] = ()
    conversation_ids: tuple[UUID, ...] = ()
    conversation_member_ids: tuple[UUID, ...] = ()
    conversation_invite_ids: tuple[UUID, ...] = ()
    generated_app_ids: tuple[UUID, ...] = ()
    generated_app_version_ids: tuple[UUID, ...] = ()
    generated_app_record_ids: tuple[UUID, ...] = ()
    generated_app_revision_ids: tuple[UUID, ...] = ()
    generated_app_build_job_ids: tuple[UUID, ...] = ()
    generated_app_membership_ids: tuple[UUID, ...] = ()
    generated_app_data_record_ids: tuple[UUID, ...] = ()
    generated_app_event_ids: tuple[UUID, ...] = ()
    generated_app_access_ticket_ids: tuple[UUID, ...] = ()
    generated_app_session_ids: tuple[UUID, ...] = ()
    auth_identity_ids: tuple[UUID, ...] = ()
    integration_account_ids: tuple[UUID, ...] = ()
    integration_grant_ids: tuple[UUID, ...] = ()
    integration_oauth_state_ids: tuple[UUID, ...] = ()
    integration_connect_link_ids: tuple[UUID, ...] = ()
    integration_subscription_ids: tuple[UUID, ...] = ()
    financial_connection_ids: tuple[UUID, ...] = ()
    financial_link_session_ids: tuple[UUID, ...] = ()
    financial_account_ids: tuple[UUID, ...] = ()
    financial_transaction_ids: tuple[UUID, ...] = ()
    financial_goal_ids: tuple[UUID, ...] = ()
    scheduled_task_ids: tuple[UUID, ...] = ()
    channel_ids: tuple[UUID, ...] = ()
    message_ids: tuple[UUID, ...] = ()
    delivery_ids: tuple[UUID, ...] = ()
    agent_run_ids: tuple[UUID, ...] = ()
    tool_call_ids: tuple[UUID, ...] = ()
    user_event_ids: tuple[UUID, ...] = ()
    memory_job_ids: tuple[UUID, ...] = ()
    memory_episode_ids: tuple[UUID, ...] = ()
    memory_entity_ids: tuple[UUID, ...] = ()
    memory_fact_ids: tuple[UUID, ...] = ()
    memory_evidence_ids: tuple[UUID, ...] = ()
    chat_external_ids: tuple[str, ...] = ()

    if user_id is not None:
        user_identifiers = (
            await session.scalars(select(UserIdentifier).where(UserIdentifier.user_id == user_id))
        ).all()
        user_identifier_ids = tuple(item.id for item in user_identifiers)
        member_handles = {normalized.value, *(item.normalized_value for item in user_identifiers)}
        auth_identity_ids = tuple(
            (
                await session.scalars(
                    select(AuthIdentity.id).where(AuthIdentity.user_id == user_id)
                )
            ).all()
        )
        integration_account_ids = tuple(
            (
                await session.scalars(
                    select(IntegrationAccount.id).where(IntegrationAccount.user_id == user_id)
                )
            ).all()
        )
        integration_oauth_state_ids = tuple(
            (
                await session.scalars(
                    select(IntegrationOAuthState.id).where(IntegrationOAuthState.user_id == user_id)
                )
            ).all()
        )
        integration_connect_link_ids = tuple(
            (
                await session.scalars(
                    select(IntegrationConnectLink.id).where(
                        IntegrationConnectLink.user_id == user_id
                    )
                )
            ).all()
        )
        financial_connection_ids = tuple(
            (
                await session.scalars(
                    select(FinancialConnection.id).where(FinancialConnection.user_id == user_id)
                )
            ).all()
        )
        financial_link_session_ids = tuple(
            (
                await session.scalars(
                    select(FinancialLinkSession.id).where(FinancialLinkSession.user_id == user_id)
                )
            ).all()
        )
        financial_transaction_ids = tuple(
            (
                await session.scalars(
                    select(FinancialTransaction.id).where(FinancialTransaction.user_id == user_id)
                )
            ).all()
        )
        financial_goal_ids = tuple(
            (
                await session.scalars(
                    select(FinancialGoal.id).where(FinancialGoal.user_id == user_id)
                )
            ).all()
        )
        scheduled_task_ids = tuple(
            (
                await session.scalars(
                    select(ScheduledTask.id).where(ScheduledTask.user_id == user_id)
                )
            ).all()
        )
        if financial_connection_ids:
            financial_account_ids = tuple(
                (
                    await session.scalars(
                        select(FinancialAccount.id).where(
                            FinancialAccount.connection_id.in_(financial_connection_ids)
                        )
                    )
                ).all()
            )
        if integration_account_ids:
            integration_grant_ids = tuple(
                (
                    await session.scalars(
                        select(IntegrationGrant.id).where(
                            IntegrationGrant.account_id.in_(integration_account_ids)
                        )
                    )
                ).all()
            )
            integration_subscription_ids = tuple(
                (
                    await session.scalars(
                        select(IntegrationSubscription.id).where(
                            IntegrationSubscription.account_id.in_(integration_account_ids)
                        )
                    )
                ).all()
            )
        conversation_ids = tuple(
            (
                await session.scalars(
                    select(Conversation.id).where(Conversation.user_id == user_id)
                )
            ).all()
        )
        conversation_member_ids = tuple(
            (
                await session.scalars(
                    select(ConversationMember.id).where(
                        or_(
                            ConversationMember.user_id == user_id,
                            ConversationMember.external_handle.in_(member_handles),
                        )
                    )
                )
            ).all()
        )
        conversation_invite_ids = tuple(
            (
                await session.scalars(
                    select(ConversationInvite.id).where(
                        or_(
                            ConversationInvite.created_by_user_id == user_id,
                            ConversationInvite.conversation_id.in_(conversation_ids),
                        )
                    )
                )
            ).all()
        )
        # A user's reset must also remove group apps attached to conversations they own. Group
        # ownership can move independently of the historical ``GeneratedApp.user_id`` during
        # participant transitions, so select both authority paths before deleting conversations.
        generated_app_ids = tuple(
            (
                await session.scalars(
                    select(GeneratedApp.id).where(
                        or_(
                            GeneratedApp.user_id == user_id,
                            GeneratedApp.conversation_id.in_(conversation_ids),
                        )
                    )
                )
            ).all()
        )
        if generated_app_ids:
            generated_app_version_ids = tuple(
                (
                    await session.scalars(
                        select(GeneratedAppVersion.id).where(
                            GeneratedAppVersion.app_id.in_(generated_app_ids)
                        )
                    )
                ).all()
            )
            generated_app_record_ids = tuple(
                (
                    await session.scalars(
                        select(GeneratedAppRecord.id).where(
                            GeneratedAppRecord.app_id.in_(generated_app_ids)
                        )
                    )
                ).all()
            )
            generated_app_revision_ids = tuple(
                (
                    await session.scalars(
                        select(GeneratedAppRevision.id).where(
                            GeneratedAppRevision.app_id.in_(generated_app_ids)
                        )
                    )
                ).all()
            )
            generated_app_build_job_ids = tuple(
                (
                    await session.scalars(
                        select(GeneratedAppBuildJob.id).where(
                            GeneratedAppBuildJob.app_id.in_(generated_app_ids)
                        )
                    )
                ).all()
            )
            generated_app_membership_ids = tuple(
                (
                    await session.scalars(
                        select(GeneratedAppMembership.id).where(
                            GeneratedAppMembership.app_id.in_(generated_app_ids)
                        )
                    )
                ).all()
            )
            generated_app_data_record_ids = tuple(
                (
                    await session.scalars(
                        select(GeneratedAppDataRecord.id).where(
                            GeneratedAppDataRecord.app_id.in_(generated_app_ids)
                        )
                    )
                ).all()
            )
            generated_app_event_ids = tuple(
                (
                    await session.scalars(
                        select(GeneratedAppEvent.id).where(
                            GeneratedAppEvent.app_id.in_(generated_app_ids)
                        )
                    )
                ).all()
            )
            generated_app_access_ticket_ids = tuple(
                (
                    await session.scalars(
                        select(GeneratedAppAccessTicket.id).where(
                            GeneratedAppAccessTicket.app_id.in_(generated_app_ids)
                        )
                    )
                ).all()
            )
            generated_app_session_ids = tuple(
                (
                    await session.scalars(
                        select(GeneratedAppSession.id).where(
                            GeneratedAppSession.app_id.in_(generated_app_ids)
                        )
                    )
                ).all()
            )
        if conversation_ids:
            channel_rows = (
                await session.execute(
                    select(ConversationChannel.id, ConversationChannel.external_id).where(
                        ConversationChannel.conversation_id.in_(conversation_ids)
                    )
                )
            ).all()
            channel_ids = tuple(row.id for row in channel_rows)
            chat_external_ids = tuple(row.external_id for row in channel_rows)

        message_ids = tuple(
            (await session.scalars(select(Message.id).where(Message.user_id == user_id))).all()
        )
        if message_ids:
            delivery_ids = tuple(
                (
                    await session.scalars(
                        select(MessageDelivery.id).where(
                            MessageDelivery.message_id.in_(message_ids)
                        )
                    )
                ).all()
            )
        agent_run_ids = tuple(
            (await session.scalars(select(AgentRun.id).where(AgentRun.user_id == user_id))).all()
        )
        user_event_ids = tuple(
            (await session.scalars(select(UserEvent.id).where(UserEvent.user_id == user_id))).all()
        )
        memory_job_ids = tuple(
            (await session.scalars(select(MemoryJob.id).where(MemoryJob.user_id == user_id))).all()
        )
        memory_episode_ids = tuple(
            (
                await session.scalars(
                    select(MemoryEpisode.id).where(MemoryEpisode.user_id == user_id)
                )
            ).all()
        )
        memory_entity_ids = tuple(
            (
                await session.scalars(
                    select(MemoryEntity.id).where(MemoryEntity.user_id == user_id)
                )
            ).all()
        )
        memory_fact_ids = tuple(
            (
                await session.scalars(select(MemoryFact.id).where(MemoryFact.user_id == user_id))
            ).all()
        )
        if memory_fact_ids or memory_episode_ids:
            memory_evidence_ids = tuple(
                (
                    await session.scalars(
                        select(MemoryEvidence.id).where(
                            or_(
                                MemoryEvidence.fact_id.in_(memory_fact_ids),
                                MemoryEvidence.episode_id.in_(memory_episode_ids),
                            )
                        )
                    )
                ).all()
            )

        if agent_run_ids:
            tool_call_ids = tuple(
                (
                    await session.scalars(
                        select(AgentToolCall.id).where(
                            AgentToolCall.agent_run_id.in_(agent_run_ids)
                        )
                    )
                ).all()
            )

    webhook_targets = {
        normalized.value,
        *chat_external_ids,
        *(
            item.normalized_value
            for item in (
                await session.scalars(
                    select(UserIdentifier).where(UserIdentifier.id.in_(user_identifier_ids))
                )
            ).all()
        ),
        *(str(account_id) for account_id in integration_account_ids),
        *(str(connection_id) for connection_id in financial_connection_ids),
    }
    webhook_events = (await session.scalars(select(WebhookEvent))).all()
    webhook_event_ids = tuple(
        event.id
        for event in webhook_events
        if _contains_exact_value(event.payload, webhook_targets)
    )

    return UserResetPlan(
        normalized_identifier=normalized.value,
        identifier_kind=normalized.kind.value,
        user_id=user_id,
        user_identifier_ids=user_identifier_ids,
        conversation_ids=conversation_ids,
        conversation_member_ids=conversation_member_ids,
        conversation_invite_ids=conversation_invite_ids,
        generated_app_ids=generated_app_ids,
        generated_app_version_ids=generated_app_version_ids,
        generated_app_record_ids=generated_app_record_ids,
        generated_app_revision_ids=generated_app_revision_ids,
        generated_app_build_job_ids=generated_app_build_job_ids,
        generated_app_membership_ids=generated_app_membership_ids,
        generated_app_data_record_ids=generated_app_data_record_ids,
        generated_app_event_ids=generated_app_event_ids,
        generated_app_access_ticket_ids=generated_app_access_ticket_ids,
        generated_app_session_ids=generated_app_session_ids,
        auth_identity_ids=auth_identity_ids,
        integration_account_ids=integration_account_ids,
        integration_grant_ids=integration_grant_ids,
        integration_oauth_state_ids=integration_oauth_state_ids,
        integration_connect_link_ids=integration_connect_link_ids,
        integration_subscription_ids=integration_subscription_ids,
        financial_connection_ids=financial_connection_ids,
        financial_link_session_ids=financial_link_session_ids,
        financial_account_ids=financial_account_ids,
        financial_transaction_ids=financial_transaction_ids,
        financial_goal_ids=financial_goal_ids,
        scheduled_task_ids=scheduled_task_ids,
        channel_ids=channel_ids,
        message_ids=message_ids,
        delivery_ids=delivery_ids,
        agent_run_ids=agent_run_ids,
        tool_call_ids=tool_call_ids,
        user_event_ids=user_event_ids,
        memory_job_ids=memory_job_ids,
        memory_episode_ids=memory_episode_ids,
        memory_entity_ids=memory_entity_ids,
        memory_fact_ids=memory_fact_ids,
        memory_evidence_ids=memory_evidence_ids,
        webhook_event_ids=webhook_event_ids,
    )


async def execute_user_reset(session: AsyncSession, plan: UserResetPlan) -> None:
    """Delete every local record in a precomputed reset plan."""
    if plan.tool_call_ids:
        await session.execute(delete(AgentToolCall).where(AgentToolCall.id.in_(plan.tool_call_ids)))
    if plan.user_event_ids:
        await session.execute(delete(UserEvent).where(UserEvent.id.in_(plan.user_event_ids)))
    if plan.memory_evidence_ids:
        await session.execute(
            delete(MemoryEvidence).where(MemoryEvidence.id.in_(plan.memory_evidence_ids))
        )
    if plan.memory_fact_ids:
        await session.execute(delete(MemoryFact).where(MemoryFact.id.in_(plan.memory_fact_ids)))
    if plan.memory_entity_ids:
        await session.execute(
            delete(MemoryEntity).where(MemoryEntity.id.in_(plan.memory_entity_ids))
        )
    if plan.memory_episode_ids:
        await session.execute(
            delete(MemoryEpisode).where(MemoryEpisode.id.in_(plan.memory_episode_ids))
        )
    if plan.memory_job_ids:
        await session.execute(delete(MemoryJob).where(MemoryJob.id.in_(plan.memory_job_ids)))
    if plan.agent_run_ids:
        await session.execute(delete(AgentRun).where(AgentRun.id.in_(plan.agent_run_ids)))
    if plan.delivery_ids:
        await session.execute(
            delete(MessageDelivery).where(MessageDelivery.id.in_(plan.delivery_ids))
        )
    if plan.message_ids:
        await session.execute(delete(Message).where(Message.id.in_(plan.message_ids)))
    if plan.channel_ids:
        await session.execute(
            delete(ConversationChannel).where(ConversationChannel.id.in_(plan.channel_ids))
        )
    if plan.generated_app_record_ids:
        await session.execute(
            delete(GeneratedAppRecord).where(
                GeneratedAppRecord.id.in_(plan.generated_app_record_ids)
            )
        )
    if plan.generated_app_event_ids:
        await session.execute(
            delete(GeneratedAppEvent).where(
                GeneratedAppEvent.id.in_(plan.generated_app_event_ids)
            )
        )
    if plan.generated_app_session_ids:
        await session.execute(
            delete(GeneratedAppSession).where(
                GeneratedAppSession.id.in_(plan.generated_app_session_ids)
            )
        )
    if plan.generated_app_access_ticket_ids:
        await session.execute(
            delete(GeneratedAppAccessTicket).where(
                GeneratedAppAccessTicket.id.in_(plan.generated_app_access_ticket_ids)
            )
        )
    if plan.generated_app_data_record_ids:
        await session.execute(
            delete(GeneratedAppDataRecord).where(
                GeneratedAppDataRecord.id.in_(plan.generated_app_data_record_ids)
            )
        )
    if plan.generated_app_build_job_ids:
        await session.execute(
            delete(GeneratedAppBuildJob).where(
                GeneratedAppBuildJob.id.in_(plan.generated_app_build_job_ids)
            )
        )
    if plan.generated_app_membership_ids:
        await session.execute(
            delete(GeneratedAppMembership).where(
                GeneratedAppMembership.id.in_(plan.generated_app_membership_ids)
            )
        )
    if plan.generated_app_ids:
        await session.execute(
            delete(GeneratedAppDeployment).where(
                GeneratedAppDeployment.app_id.in_(plan.generated_app_ids)
            )
        )
    if plan.generated_app_revision_ids:
        await session.execute(
            delete(GeneratedAppRevision).where(
                GeneratedAppRevision.id.in_(plan.generated_app_revision_ids)
            )
        )
    if plan.generated_app_version_ids:
        await session.execute(
            delete(GeneratedAppVersion).where(
                GeneratedAppVersion.id.in_(plan.generated_app_version_ids)
            )
        )
    if plan.generated_app_ids:
        await session.execute(
            delete(GeneratedApp).where(GeneratedApp.id.in_(plan.generated_app_ids))
        )
    if plan.integration_subscription_ids:
        await session.execute(
            delete(IntegrationSubscription).where(
                IntegrationSubscription.id.in_(plan.integration_subscription_ids)
            )
        )
    if plan.integration_grant_ids:
        await session.execute(
            delete(IntegrationGrant).where(IntegrationGrant.id.in_(plan.integration_grant_ids))
        )
    if plan.integration_oauth_state_ids:
        await session.execute(
            delete(IntegrationOAuthState).where(
                IntegrationOAuthState.id.in_(plan.integration_oauth_state_ids)
            )
        )
    if plan.integration_connect_link_ids:
        await session.execute(
            delete(IntegrationConnectLink).where(
                IntegrationConnectLink.id.in_(plan.integration_connect_link_ids)
            )
        )
    if plan.integration_account_ids:
        await session.execute(
            delete(IntegrationAccount).where(
                IntegrationAccount.id.in_(plan.integration_account_ids)
            )
        )
    if plan.financial_goal_ids:
        await session.execute(
            delete(FinancialGoal).where(FinancialGoal.id.in_(plan.financial_goal_ids))
        )
    if plan.financial_transaction_ids:
        await session.execute(
            delete(FinancialTransaction).where(
                FinancialTransaction.id.in_(plan.financial_transaction_ids)
            )
        )
    if plan.financial_account_ids:
        await session.execute(
            delete(FinancialAccount).where(FinancialAccount.id.in_(plan.financial_account_ids))
        )
    if plan.financial_connection_ids:
        await session.execute(
            delete(FinancialConnection).where(
                FinancialConnection.id.in_(plan.financial_connection_ids)
            )
        )
    if plan.financial_link_session_ids:
        await session.execute(
            delete(FinancialLinkSession).where(
                FinancialLinkSession.id.in_(plan.financial_link_session_ids)
            )
        )
    if plan.scheduled_task_ids:
        await session.execute(
            delete(ScheduledTask).where(ScheduledTask.id.in_(plan.scheduled_task_ids))
        )
    if plan.auth_identity_ids:
        await session.execute(
            delete(AuthIdentity).where(AuthIdentity.id.in_(plan.auth_identity_ids))
        )
    if plan.user_identifier_ids:
        await session.execute(
            delete(UserIdentifier).where(UserIdentifier.id.in_(plan.user_identifier_ids))
        )
    if plan.conversation_invite_ids:
        await session.execute(
            delete(ConversationInvite).where(
                ConversationInvite.id.in_(plan.conversation_invite_ids)
            )
        )
    if plan.conversation_member_ids:
        await session.execute(
            delete(ConversationMember).where(
                ConversationMember.id.in_(plan.conversation_member_ids)
            )
        )
    if plan.conversation_ids:
        await session.execute(
            delete(Conversation).where(Conversation.id.in_(plan.conversation_ids))
        )
    if plan.webhook_event_ids:
        await session.execute(
            delete(WebhookEvent).where(WebhookEvent.id.in_(plan.webhook_event_ids))
        )
    if plan.user_id is not None:
        await session.execute(delete(User).where(User.id == plan.user_id))
