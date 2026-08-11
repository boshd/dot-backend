from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.db.session import get_session
from benji_api.schemas.phone import PhoneNumber
from benji_api.services.users import resolve_user_from_phone

router = APIRouter(prefix="/inbound/messages", tags=["inbound messages"])


class InboundMessageCreate(BaseModel):
    sender_phone_number: PhoneNumber
    conversation_id: UUID | None = None
    channel: str = Field(min_length=1, max_length=64, examples=["linq"])
    content: str = Field(min_length=1, max_length=20_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class InboundMessageReceipt(BaseModel):
    message_id: UUID
    conversation_id: UUID
    user_id: UUID
    user_created: bool
    onboarding_step: str
    status: Literal["received"] = "received"


@router.post("", response_model=InboundMessageReceipt, status_code=status.HTTP_202_ACCEPTED)
async def receive_inbound_message(
    message: InboundMessageCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InboundMessageReceipt:
    """Trusted adapter ingress: resolve the sender before message orchestration."""
    resolution = await resolve_user_from_phone(session, message.sender_phone_number)
    await session.commit()

    return InboundMessageReceipt(
        message_id=uuid4(),
        conversation_id=message.conversation_id or uuid4(),
        user_id=resolution.user.id,
        user_created=resolution.created,
        onboarding_step=resolution.user.onboarding_step,
    )
