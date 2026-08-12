from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.tools import (
    CancelAccountDeletionTool,
    DeleteAccountTool,
    GetAccountSettingsTool,
    UpdateAccountSettingTool,
)
from benji_api.agents.types import ToolContext
from benji_api.config import Settings
from benji_api.db.base import Base
from benji_api.models import (
    Conversation,
    ConversationKind,
    ConversationMember,
    ConversationMemberRole,
    GeneratedApp,
    GeneratedAppAccessTicket,
    GeneratedAppMembership,
    GeneratedAppSession,
    Message,
    MessageAttachment,
    MessageDirection,
    MessageStatus,
    ScheduledTask,
    User,
    UserIdentifier,
)
from benji_api.models.user import OnboardingStatus, OnboardingStep
from benji_api.services.schedules import dispatch_due_scheduled_task


class FakeLinqAttachmentClient:
    def __init__(self) -> None:
        self.deleted_attachment_ids: list[str] = []

    async def delete_attachment(self, *, attachment_id: str) -> None:
        self.deleted_attachment_ids.append(attachment_id)


@pytest.mark.anyio
async def test_direct_user_can_inspect_and_update_account_settings() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        user = User(
            phone_number="+14155552671",
            display_name="Kareem",
            birth_date=date(1996, 9, 2),
            location_city="Cairo",
            location_country="Egypt",
            location_text="Cairo, Egypt",
            onboarding_status=OnboardingStatus.COMPLETE.value,
            onboarding_step=OnboardingStep.COMPLETE.value,
        )
        session.add(user)
        await session.flush()
        direct = Conversation(user_id=user.id)
        group = Conversation(user_id=user.id, kind=ConversationKind.GROUP.value)
        session.add_all([direct, group])
        await session.commit()

    context = ToolContext(user_id=user.id, conversation_id=direct.id)
    settings = await GetAccountSettingsTool(session_factory=factory).execute(
        context=context,
        arguments={},
    )
    assert settings == {
        "display_name": "Kareem",
        "birth_date": "1996-09-02",
        "location_city": "Cairo",
        "location_country": "Egypt",
        "preferred_language_mode": "auto",
        "messaging_enabled": True,
    }

    update_tool = UpdateAccountSettingTool(session_factory=factory)
    renamed = await update_tool.execute(
        context=context,
        arguments={"field": "display_name", "value": "Kimo"},
    )
    assert renamed["settings"]["display_name"] == "Kimo"
    language = await update_tool.execute(
        context=context,
        arguments={"field": "preferred_language_mode", "value": "egyptian_franco"},
    )
    assert language["settings"]["preferred_language_mode"] == "egyptian_franco"
    with pytest.raises(ValueError, match="only available"):
        await update_tool.execute(
            context=ToolContext(user_id=user.id, conversation_id=group.id),
            arguments={"field": "display_name", "value": "Private leak"},
        )
    await engine.dispose()


@pytest.mark.anyio
async def test_account_deletion_requires_exact_message_and_preserves_shared_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = datetime.now(UTC)
    async with factory() as session:
        user = User(
            phone_number="+14155552671",
            display_name="Kareem",
            birth_date=date(1996, 9, 2),
            location_country="Egypt",
            onboarding_status=OnboardingStatus.COMPLETE.value,
            onboarding_step=OnboardingStep.COMPLETE.value,
        )
        replacement_user = User(phone_number="+14155552672", display_name="Maya")
        session.add_all([user, replacement_user])
        await session.flush()
        session.add(
            UserIdentifier(
                user_id=user.id,
                kind="phone",
                normalized_value=user.phone_number,
                display_value=user.phone_number,
                source="linq",
                is_primary=True,
            )
        )
        direct = Conversation(user_id=user.id)
        group = Conversation(
            user_id=user.id,
            kind=ConversationKind.GROUP.value,
            title="Trip",
            group_owner_source="explicit",
        )
        session.add_all([direct, group])
        await session.flush()
        session.add_all(
            [
                ConversationMember(
                    conversation_id=group.id,
                    user_id=user.id,
                    external_handle=user.phone_number,
                    display_name="Kareem",
                    role=ConversationMemberRole.OWNER.value,
                ),
                ConversationMember(
                    conversation_id=group.id,
                    user_id=replacement_user.id,
                    external_handle=replacement_user.phone_number,
                    display_name="Maya",
                ),
            ]
        )
        initial_request = Message(
            conversation_id=direct.id,
            user_id=user.id,
            sender_user_id=user.id,
            source_channel="linq",
            direction=MessageDirection.INBOUND.value,
            status=MessageStatus.RECEIVED.value,
            content="can you delete my account?",
            created_at=now,
        )
        group_message = Message(
            conversation_id=group.id,
            user_id=user.id,
            sender_user_id=replacement_user.id,
            source_channel="linq",
            direction=MessageDirection.INBOUND.value,
            status=MessageStatus.RECEIVED.value,
            content="the cottage is booked",
            created_at=now,
        )
        shared_app = GeneratedApp(
            user_id=user.id,
            conversation_id=group.id,
            public_id="shared-trip-app-token-123456",
            title="Trip split",
            description="",
            template="code_app",
            theme="dot",
            access_mode="collaborative_link",
            runtime_kind="code",
            current_version=0,
        )
        session.add_all([initial_request, group_message, shared_app])
        await session.flush()
        shared_ticket = GeneratedAppAccessTicket(
            app_id=shared_app.id,
            issued_by_user_id=user.id,
            role="member",
            token_hash="6" * 64,
            expires_at=now + timedelta(days=1),
        )
        shared_session = GeneratedAppSession(
            app_id=shared_app.id,
            role="member",
            token_hash="7" * 64,
            expires_at=now + timedelta(days=1),
        )
        session.add_all(
            [
                GeneratedAppMembership(
                    app_id=shared_app.id,
                    user_id=user.id,
                    role="owner",
                ),
                shared_ticket,
                shared_session,
                MessageAttachment(
                    message_id=initial_request.id,
                    provider="linq",
                    provider_attachment_id="private-attachment",
                    part_index=0,
                ),
                MessageAttachment(
                    message_id=initial_request.id,
                    provider="linq",
                    provider_attachment_id="private-attachment",
                    part_index=1,
                ),
                MessageAttachment(
                    message_id=group_message.id,
                    provider="linq",
                    provider_attachment_id="shared-group-attachment",
                    part_index=0,
                ),
            ]
        )
        await session.commit()

    context = ToolContext(user_id=user.id, conversation_id=direct.id)
    deletion_tool = DeleteAccountTool(session_factory=factory)
    first = await deletion_tool.execute(context=context, arguments={})
    assert first["deletion_scheduled"] is False
    assert first["confirmation_phrase"] == "delete my dot account forever"

    async with factory() as session:
        session.add(
            Message(
                conversation_id=direct.id,
                user_id=user.id,
                sender_user_id=user.id,
                source_channel="linq",
                direction=MessageDirection.INBOUND.value,
                status=MessageStatus.RECEIVED.value,
                content="delete my dot account forever",
                created_at=now + timedelta(seconds=1),
            )
        )
        await session.commit()
    scheduled = await deletion_tool.execute(context=context, arguments={})
    assert scheduled["deletion_scheduled"] is True
    assert scheduled["grace_seconds"] == 60

    cancelled = await CancelAccountDeletionTool(session_factory=factory).execute(
        context=context,
        arguments={},
    )
    assert cancelled == {"cancelled": True}

    # A later exact confirmation can schedule a fresh deletion after cancellation.
    async with factory() as session:
        session.add(
            Message(
                conversation_id=direct.id,
                user_id=user.id,
                sender_user_id=user.id,
                source_channel="linq",
                direction=MessageDirection.INBOUND.value,
                status=MessageStatus.RECEIVED.value,
                content="delete my dot account forever.",
                created_at=now + timedelta(seconds=2),
            )
        )
        await session.commit()
    assert (await deletion_tool.execute(context=context, arguments={}))[
        "deletion_scheduled"
    ] is True
    async with factory() as session:
        active_task = await session.scalar(
            select(ScheduledTask).where(ScheduledTask.status == "active")
        )
        assert active_task is not None
        active_task.scheduled_for = now - timedelta(seconds=1)
        active_task.next_attempt_at = now - timedelta(seconds=1)
        await session.commit()

    fake_linq = FakeLinqAttachmentClient()
    monkeypatch.setattr(
        "benji_api.services.account_management.LinqClient",
        lambda **_: fake_linq,
    )
    assert await dispatch_due_scheduled_task(
        settings=Settings(linq_api_key="test-key"),
        session_factory=factory,
    )
    assert fake_linq.deleted_attachment_ids == ["private-attachment"]
    async with factory() as session:
        assert await session.get(User, user.id) is None
        transferred_group = await session.get(Conversation, group.id)
        assert transferred_group is not None
        assert transferred_group.user_id == replacement_user.id
        assert transferred_group.group_owner_source == "transferred"
        assert (await session.get(GeneratedApp, shared_app.id)).user_id == replacement_user.id
        replacement_app_membership = await session.scalar(
            select(GeneratedAppMembership).where(
                GeneratedAppMembership.app_id == shared_app.id,
                GeneratedAppMembership.user_id == replacement_user.id,
            )
        )
        assert replacement_app_membership is not None
        assert replacement_app_membership.role == "owner"
        preserved_ticket = await session.get(GeneratedAppAccessTicket, shared_ticket.id)
        assert preserved_ticket is not None
        assert preserved_ticket.issued_by_user_id == replacement_user.id
        preserved_session = await session.get(GeneratedAppSession, shared_session.id)
        assert preserved_session is not None
        assert preserved_session.revoked_at is None
        assert (
            await session.scalar(
                select(func.count()).select_from(Message).where(Message.conversation_id == group.id)
            )
            == 1
        )
        remaining_attachments = list((await session.scalars(select(MessageAttachment))).all())
        assert [attachment.provider_attachment_id for attachment in remaining_attachments] == [
            "shared-group-attachment"
        ]
        replacement_member = await session.scalar(
            select(ConversationMember).where(
                ConversationMember.conversation_id == group.id,
                ConversationMember.user_id == replacement_user.id,
            )
        )
        assert replacement_member is not None
        assert replacement_member.role == ConversationMemberRole.OWNER.value
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ScheduledTask)
                .where(ScheduledTask.user_id == user.id)
            )
            == 0
        )
    await engine.dispose()
