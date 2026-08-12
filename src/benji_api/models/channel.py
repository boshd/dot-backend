from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from benji_api.db.base import Base
from benji_api.models.user import utc_now


class ConversationKind(StrEnum):
    DIRECT = "direct"
    GROUP = "group"


class ConversationMemberRole(StrEnum):
    OWNER = "owner"
    MEMBER = "member"


class ConversationMemberStatus(StrEnum):
    ACTIVE = "active"
    LEFT = "left"
    REMOVED = "removed"


class GroupResponseMode(StrEnum):
    AUTO = "auto"
    MENTIONS = "mentions"
    ALWAYS = "always"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageStatus(StrEnum):
    RECEIVED = "received"
    COMPLETED = "completed"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class WebhookStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_user_id", "user_id"),
        Index(
            "uq_conversations_direct_user",
            "user_id",
            unique=True,
            postgresql_where=text("kind = 'direct'"),
            sqlite_where=text("kind = 'direct'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(
        String(32), default=ConversationKind.DIRECT.value, nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(120))
    avatar_url: Mapped[str | None] = mapped_column(String(2_048))
    response_mode: Mapped[str] = mapped_column(
        String(32), default=GroupResponseMode.AUTO.value, nullable=False
    )
    group_owner_source: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ConversationMember(Base):
    __tablename__ = "conversation_members"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "external_handle", name="uq_conversation_members_handle"
        ),
        UniqueConstraint("conversation_id", "user_id", name="uq_conversation_members_user"),
        Index("ix_conversation_members_conversation_id", "conversation_id"),
        Index("ix_conversation_members_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    external_handle: Mapped[str] = mapped_column(String(255), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(120))
    service: Mapped[str | None] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(
        String(32), default=ConversationMemberRole.MEMBER.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default=ConversationMemberStatus.ACTIVE.value, nullable=False
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ConversationInvite(Base):
    __tablename__ = "conversation_invites"
    __table_args__ = (
        Index("ix_conversation_invites_conversation_id", "conversation_id"),
        Index("ix_conversation_invites_token_hash", "token_hash", unique=True),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ConversationChannel(Base):
    __tablename__ = "conversation_channels"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_conversation_channels_provider_id"),
        Index("ix_conversation_channels_conversation_id", "conversation_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    service: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "source_channel", "source_external_id", name="uq_messages_source_external_id"
        ),
        UniqueConstraint(
            "source_channel", "idempotency_key", name="uq_messages_source_idempotency"
        ),
        Index("ix_messages_conversation_id", "conversation_id"),
        Index("ix_messages_user_id", "user_id"),
        Index("ix_messages_sender_user_id", "sender_user_id"),
        Index("ix_messages_source_binding_id", "source_binding_id"),
        Index("ix_messages_response_group", "response_group_id", "response_ordinal"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    sender_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    source_binding_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversation_channels.id", ondelete="SET NULL")
    )
    source_channel: Mapped[str] = mapped_column(String(32), nullable=False)
    source_external_id: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    response_group_id: Mapped[UUID | None] = mapped_column()
    response_ordinal: Mapped[int | None] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class MessageAttachment(Base):
    __tablename__ = "message_attachments"
    __table_args__ = (
        UniqueConstraint("message_id", "part_index", name="uq_message_attachments_part"),
        Index("ix_message_attachments_message_id", "message_id"),
        Index(
            "ix_message_attachments_provider_id",
            "provider",
            "provider_attachment_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_attachment_id: Mapped[str | None] = mapped_column(String(255))
    part_index: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="media", nullable=False)
    filename: Mapped[str | None] = mapped_column(String(512))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_url_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class MessageDelivery(Base):
    __tablename__ = "message_deliveries"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_message_deliveries_provider_id"),
        UniqueConstraint(
            "provider", "idempotency_key", name="uq_message_deliveries_provider_idempotency"
        ),
        Index("ix_message_deliveries_message_id", "message_id"),
        Index("ix_message_deliveries_channel_id", "channel_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversation_channels.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), default=DeliveryStatus.PENDING.value, nullable=False
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "external_event_id", name="uq_webhook_events_provider_id"),
        Index("ix_webhook_events_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), default=WebhookStatus.RECEIVED.value, nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
