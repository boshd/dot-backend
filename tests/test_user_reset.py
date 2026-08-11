from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.db.base import Base
from benji_api.models import (
    AgentRun,
    AgentRunStatus,
    AgentToolCall,
    AuthIdentity,
    Conversation,
    ConversationChannel,
    DeliveryStatus,
    FinancialAccount,
    FinancialConnection,
    FinancialGoal,
    FinancialLinkSession,
    FinancialTransaction,
    GeneratedApp,
    GeneratedAppRecord,
    GeneratedAppVersion,
    Message,
    MessageDelivery,
    MessageDirection,
    MessageStatus,
    ScheduledTask,
    ToolCallStatus,
    User,
    UserEvent,
    WebhookEvent,
)
from benji_api.services.user_reset import build_user_reset_plan, execute_user_reset


@pytest.mark.anyio
async def test_user_reset_deletes_related_local_data_only() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    phone = "+14155552671"
    chat_id = "chat-for-reset"
    async with session_factory() as session:
        user = User(phone_number=phone)
        session.add(user)
        await session.flush()

        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()

        generated_app = GeneratedApp(
            user_id=user.id,
            conversation_id=conversation.id,
            public_id="reset-test-public-app-id-1234",
            title="Reset me",
            description="",
            template="checklist",
            theme="coral",
            access_mode="private_link",
        )
        session.add(generated_app)
        await session.flush()
        session.add(
            GeneratedAppVersion(
                app_id=generated_app.id,
                version=1,
                specification={"schema_version": 1},
            )
        )
        session.add(
            GeneratedAppRecord(
                app_id=generated_app.id,
                kind="item",
                data={"text": "temporary", "completed": False},
            )
        )

        channel = ConversationChannel(
            conversation_id=conversation.id,
            provider="linq",
            external_id=chat_id,
        )
        session.add(channel)
        await session.flush()

        message = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            source_binding_id=channel.id,
            source_channel="linq",
            source_external_id="message-for-reset",
            direction=MessageDirection.INBOUND.value,
            status=MessageStatus.RECEIVED.value,
            content="hello",
        )
        session.add(message)
        await session.flush()

        agent_run = AgentRun(
            conversation_id=conversation.id,
            user_id=user.id,
            trigger_message_id=message.id,
            provider="openai",
            model="test-model",
            status=AgentRunStatus.COMPLETED.value,
        )
        session.add(agent_run)
        await session.flush()

        session.add(
            MessageDelivery(
                message_id=message.id,
                channel_id=channel.id,
                provider="linq",
                external_id="delivery-for-reset",
                status=DeliveryStatus.DELIVERED.value,
            )
        )
        session.add(
            AuthIdentity(
                user_id=user.id,
                provider="stytch",
                provider_subject="user-test-reset",
                verified_phone=phone,
            )
        )
        session.add(
            UserEvent(
                user_id=user.id,
                event_type="integration.connected",
                source="test",
                idempotency_key="reset-user-event",
                payload={"integration_key": "google_calendar"},
            )
        )

        schedule = ScheduledTask(
            user_id=user.id,
            conversation_id=conversation.id,
            action_type="agent.reachout",
            source="test",
            idempotency_key="reset-schedule",
            title="Reset schedule",
            payload={"goal": "test"},
            scheduled_for=datetime.now(UTC) + timedelta(days=1),
            next_attempt_at=datetime.now(UTC) + timedelta(days=1),
        )
        session.add(schedule)
        financial_connection = FinancialConnection(
            user_id=user.id,
            provider="plaid",
            provider_connection_id="reset-item",
            institution_name="Reset Bank",
            credentials_ciphertext="encrypted",
        )
        session.add(financial_connection)
        session.add(
            FinancialLinkSession(
                user_id=user.id,
                provider="plaid",
                exchange_token_hash="reset-token-hash",
                initiated_channel="web",
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )
        await session.flush()
        financial_account = FinancialAccount(
            connection_id=financial_connection.id,
            provider_account_id="reset-account",
            name="Checking",
            account_type="depository",
        )
        session.add(financial_account)
        await session.flush()
        session.add(
            FinancialTransaction(
                user_id=user.id,
                account_id=financial_account.id,
                source="plaid",
                provider_transaction_id="reset-transaction",
                amount=Decimal("10"),
                transaction_date=datetime.now(UTC).date(),
                name="Reset purchase",
            )
        )
        session.add(
            FinancialGoal(
                user_id=user.id,
                conversation_id=conversation.id,
                schedule_id=schedule.id,
                title="Reset goal",
                target_amount=Decimal("100"),
                currency="USD",
                target_date=datetime.now(UTC).date() + timedelta(days=30),
            )
        )

        session.add_all(
            [
                AgentToolCall(
                    agent_run_id=agent_run.id,
                    external_call_id="call-for-reset",
                    tool_name="test_tool",
                    status=ToolCallStatus.COMPLETED.value,
                ),
                WebhookEvent(
                    provider="linq",
                    external_event_id="phone-event",
                    event_type="message.received",
                    payload={"sender": {"handle": phone}},
                ),
                WebhookEvent(
                    provider="linq",
                    external_event_id="chat-event",
                    event_type="message.delivered",
                    payload={"data": {"chat": {"id": chat_id}}},
                ),
                WebhookEvent(
                    provider="linq",
                    external_event_id="unrelated-event",
                    event_type="message.received",
                    payload={"text": f"the tester is {phone}"},
                ),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        plan = await build_user_reset_plan(session, phone)

        assert plan.user_id == user.id
        assert len(plan.auth_identity_ids) == 1
        assert len(plan.conversation_ids) == 1
        assert len(plan.generated_app_ids) == 1
        assert len(plan.generated_app_version_ids) == 1
        assert len(plan.generated_app_record_ids) == 1
        assert len(plan.channel_ids) == 1
        assert len(plan.message_ids) == 1
        assert len(plan.delivery_ids) == 1
        assert len(plan.agent_run_ids) == 1
        assert len(plan.tool_call_ids) == 1
        assert len(plan.user_event_ids) == 1
        assert len(plan.financial_connection_ids) == 1
        assert len(plan.financial_link_session_ids) == 1
        assert len(plan.financial_account_ids) == 1
        assert len(plan.financial_transaction_ids) == 1
        assert len(plan.financial_goal_ids) == 1
        assert len(plan.scheduled_task_ids) == 1
        assert len(plan.webhook_event_ids) == 2
        assert plan.total_records == 20

        await execute_user_reset(session, plan)
        await session.commit()

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 0
        assert await session.scalar(select(func.count()).select_from(AuthIdentity)) == 0
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 0
        assert await session.scalar(select(func.count()).select_from(GeneratedApp)) == 0
        assert await session.scalar(select(func.count()).select_from(GeneratedAppVersion)) == 0
        assert await session.scalar(select(func.count()).select_from(GeneratedAppRecord)) == 0
        assert await session.scalar(select(func.count()).select_from(ConversationChannel)) == 0
        assert await session.scalar(select(func.count()).select_from(Message)) == 0
        assert await session.scalar(select(func.count()).select_from(MessageDelivery)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentToolCall)) == 0
        assert await session.scalar(select(func.count()).select_from(UserEvent)) == 0
        assert await session.scalar(select(func.count()).select_from(FinancialConnection)) == 0
        assert await session.scalar(select(func.count()).select_from(FinancialAccount)) == 0
        assert await session.scalar(select(func.count()).select_from(FinancialTransaction)) == 0
        assert await session.scalar(select(func.count()).select_from(FinancialGoal)) == 0
        assert await session.scalar(select(func.count()).select_from(ScheduledTask)) == 0
        remaining_events = (await session.scalars(select(WebhookEvent))).all()
        assert [event.external_event_id for event in remaining_events] == ["unrelated-event"]

    await engine.dispose()


@pytest.mark.anyio
async def test_user_reset_can_remove_orphaned_webhook_events_by_phone() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    phone = "+14155552671"
    async with session_factory() as session:
        session.add(
            WebhookEvent(
                provider="linq",
                external_event_id="orphaned-event",
                event_type="message.received",
                payload={"participants": [{"handle": phone}]},
            )
        )
        await session.commit()

        plan = await build_user_reset_plan(session, phone)
        assert plan.user_id is None
        assert len(plan.webhook_event_ids) == 1

        await execute_user_reset(session, plan)
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 0

    await engine.dispose()
