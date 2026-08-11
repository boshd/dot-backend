from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from benji_api.db.base import Base
from benji_api.models.user import utc_now


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolCallStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class AgentRunPurpose(StrEnum):
    CONVERSATION = "conversation"
    ONBOARDING = "onboarding"
    EVENT = "event"
    FOLLOW_UP = "follow_up"


class AgentFollowUpStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_conversation_id", "conversation_id"),
        Index("ix_agent_runs_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    trigger_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE")
    )
    trigger_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user_events.id", ondelete="SET NULL")
    )
    wake_type: Mapped[str] = mapped_column(String(64), default="user_message", nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(32), default=AgentRunPurpose.CONVERSATION.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default=AgentRunStatus.RUNNING.value, nullable=False
    )
    input_message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    model_response_id: Mapped[str | None] = mapped_column(String(255))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    prompt_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    retrieved_memory: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    exposed_tools: Mapped[list[str] | None] = mapped_column(JSON)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32))
    raw_output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"
    __table_args__ = (Index("ix_agent_tool_calls_agent_run_id", "agent_run_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    agent_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    external_call_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AgentFollowUp(Base):
    __tablename__ = "agent_follow_ups"
    __table_args__ = (
        Index("ix_agent_follow_ups_user_id", "user_id"),
        Index("ix_agent_follow_ups_status_due", "status", "due_at"),
        Index("ix_agent_follow_ups_conversation_status", "conversation_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_agent_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_provider: Mapped[str | None] = mapped_column(String(32))
    cancel_on_user_message: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    chain_depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=AgentFollowUpStatus.PENDING.value, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
