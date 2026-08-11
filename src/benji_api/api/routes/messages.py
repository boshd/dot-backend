from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/messages", tags=["messages"])


class MessageCreate(BaseModel):
    user_id: UUID
    conversation_id: UUID | None = None
    channel: str = Field(min_length=1, max_length=64, examples=["web", "ios", "linq"])
    content: str = Field(min_length=1, max_length=20_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MessageReceipt(BaseModel):
    message_id: UUID
    conversation_id: UUID
    status: Literal["received"] = "received"


@router.post("", response_model=MessageReceipt, status_code=status.HTTP_202_ACCEPTED)
async def receive_message(message: MessageCreate) -> MessageReceipt:
    """Normalize message ingress before persistence and orchestration are added."""
    return MessageReceipt(
        message_id=uuid4(),
        conversation_id=message.conversation_id or uuid4(),
    )
