from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.agents.dependencies import get_model_provider, get_tool_registry
from benji_api.agents.tools import ToolRegistry
from benji_api.agents.types import ModelProvider
from benji_api.api.dependencies import get_optional_authenticated_user, resolve_client_user
from benji_api.config import Settings, get_settings
from benji_api.db.session import get_session
from benji_api.memory.dependencies import get_embedding_provider
from benji_api.memory.types import EmbeddingProvider
from benji_api.models.channel import Message, MessageDirection
from benji_api.models.user import User
from benji_api.schemas.phone import PhoneNumber
from benji_api.services.groups import list_conversation_members, member_label
from benji_api.services.web_chat import (
    WebChatConversationNotFoundError,
    open_web_chat_session,
    send_web_chat_message,
)

router = APIRouter(prefix="/web/chat", tags=["web chat"])


class WebChatMessage(BaseModel):
    id: UUID
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    sender_user_id: UUID | None = None
    sender_display_name: str | None = None
    is_current_user: bool = False


class WebChatMember(BaseModel):
    user_id: UUID | None
    display_name: str
    role: str


class WebChatIdentity(BaseModel):
    user_id: UUID
    display_name: str | None
    onboarding_status: str


class WebChatSessionCreate(BaseModel):
    phone_number: PhoneNumber | None = None
    conversation_id: UUID | None = None


class WebChatSessionResponse(BaseModel):
    conversation_id: UUID
    conversation_kind: Literal["direct", "group"]
    conversation_title: str
    user: WebChatIdentity
    messages: list[WebChatMessage]
    members: list[WebChatMember]


class WebChatMessageCreate(BaseModel):
    phone_number: PhoneNumber | None = None
    conversation_id: UUID
    client_message_id: UUID
    content: str = Field(min_length=1, max_length=20_000)


class WebChatTurnResponse(BaseModel):
    conversation_id: UUID
    user: WebChatIdentity
    assistant_message: WebChatMessage | None
    assistant_messages: list[WebChatMessage]
    replied: bool


async def _resolve_web_user(
    session: AsyncSession,
    *,
    authenticated_user: User | None,
    phone_number: str | None,
    settings: Settings,
) -> tuple[User, bool]:
    user = await resolve_client_user(
        session,
        authenticated_user=authenticated_user,
        phone_number=phone_number,
        settings=settings,
    )
    return user, False


async def _message_response(
    session: AsyncSession,
    message: Message,
    *,
    current_user_id: UUID,
) -> WebChatMessage:
    sender = (
        await session.get(User, message.sender_user_id)
        if message.sender_user_id is not None
        else None
    )
    return WebChatMessage(
        id=message.id,
        role=("user" if message.direction == MessageDirection.INBOUND.value else "assistant"),
        content=message.content,
        created_at=message.created_at,
        sender_user_id=message.sender_user_id,
        sender_display_name=sender.display_name if sender is not None else None,
        is_current_user=message.sender_user_id == current_user_id,
    )


@router.post("/session", response_model=WebChatSessionResponse)
async def create_web_chat_session(
    request: WebChatSessionCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
) -> WebChatSessionResponse:
    user, user_created = await _resolve_web_user(
        session,
        authenticated_user=authenticated_user,
        phone_number=request.phone_number,
        settings=settings,
    )
    try:
        result = await open_web_chat_session(
            session,
            user=user,
            user_created=user_created,
            conversation_id=request.conversation_id,
        )
    except WebChatConversationNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    member_rows = await list_conversation_members(session, conversation_id=result.conversation.id)
    return WebChatSessionResponse(
        conversation_id=result.conversation.id,
        conversation_kind=result.conversation.kind,
        conversation_title=(
            result.conversation.title
            if result.conversation.kind == "group" and result.conversation.title
            else "dot"
        ),
        user=WebChatIdentity(
            user_id=result.user.id,
            display_name=result.user.display_name,
            onboarding_status=result.user.onboarding_status,
        ),
        messages=[
            await _message_response(session, message, current_user_id=result.user.id)
            for message in result.messages
        ],
        members=[
            WebChatMember(
                user_id=member.user_id,
                display_name=member_label(member, member_user),
                role=member.role,
            )
            for member, member_user in member_rows
        ],
    )


@router.post("/messages", response_model=WebChatTurnResponse)
async def create_web_chat_message(
    request: WebChatMessageCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    model_provider: Annotated[ModelProvider | None, Depends(get_model_provider)],
    tool_registry: Annotated[ToolRegistry, Depends(get_tool_registry)],
    embedding_provider: Annotated[EmbeddingProvider | None, Depends(get_embedding_provider)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
) -> WebChatTurnResponse:
    user, _ = await _resolve_web_user(
        session,
        authenticated_user=authenticated_user,
        phone_number=request.phone_number,
        settings=settings,
    )
    if model_provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The model provider is not configured",
        )
    try:
        result = await send_web_chat_message(
            session,
            user=user,
            conversation_id=request.conversation_id,
            client_message_id=request.client_message_id,
            content=request.content,
            provider=model_provider,
            tools=tool_registry,
            settings=settings,
            embedding_provider=embedding_provider,
        )
    except WebChatConversationNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Dot could not complete this turn",
        ) from error
    assistant_messages = [
        await _message_response(session, message, current_user_id=result.user.id)
        for message in result.assistant_messages
    ]
    return WebChatTurnResponse(
        conversation_id=result.conversation.id,
        user=WebChatIdentity(
            user_id=result.user.id,
            display_name=result.user.display_name,
            onboarding_status=result.user.onboarding_status,
        ),
        assistant_message=assistant_messages[0] if assistant_messages else None,
        assistant_messages=assistant_messages,
        replied=bool(assistant_messages),
    )
