from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.api.dependencies import get_optional_authenticated_user, resolve_client_user
from benji_api.config import Settings, get_settings
from benji_api.db.session import get_session
from benji_api.models.channel import Conversation
from benji_api.models.user import User
from benji_api.schemas.phone import PhoneNumber
from benji_api.services.channels import resolve_direct_conversation
from benji_api.services.groups import (
    GroupInviteError,
    GroupNotFoundError,
    GroupPermissionError,
    create_group_invite,
    create_web_group,
    join_group_from_invite,
    leave_web_group,
    list_conversation_members,
    list_user_conversations,
    member_label,
    rename_group,
)

router = APIRouter(prefix="/web/conversations", tags=["web groups"])


class ConversationMemberResponse(BaseModel):
    user_id: UUID | None
    display_name: str
    role: str


class ConversationResponse(BaseModel):
    id: UUID
    kind: Literal["direct", "group"]
    title: str
    updated_at: datetime
    members: list[ConversationMemberResponse]


class ConversationListResponse(BaseModel):
    conversations: list[ConversationResponse]


class GroupCreate(BaseModel):
    phone_number: PhoneNumber | None = None
    title: str = Field(min_length=1, max_length=120)


class GroupUpdate(BaseModel):
    phone_number: PhoneNumber | None = None
    title: str = Field(min_length=1, max_length=120)


class GroupAction(BaseModel):
    phone_number: PhoneNumber | None = None


class GroupJoin(GroupAction):
    token: str = Field(min_length=20, max_length=200)


class GroupInviteResponse(BaseModel):
    invite_url: str
    expires_at: datetime


async def _user(
    session: AsyncSession,
    *,
    authenticated_user: User | None,
    phone_number: str | None,
    settings: Settings,
) -> User:
    return await resolve_client_user(
        session,
        authenticated_user=authenticated_user,
        phone_number=phone_number,
        settings=settings,
    )


async def _conversation_response(
    session: AsyncSession,
    conversation: Conversation,
) -> ConversationResponse:
    members = await list_conversation_members(session, conversation_id=conversation.id)
    return ConversationResponse(
        id=conversation.id,
        kind=conversation.kind,
        title=(
            conversation.title if conversation.kind == "group" and conversation.title else "dot"
        ),
        updated_at=conversation.updated_at,
        members=[
            ConversationMemberResponse(
                user_id=member.user_id,
                display_name=member_label(member, member_user),
                role=member.role,
            )
            for member, member_user in members
        ],
    )


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
    phone_number: Annotated[PhoneNumber | None, Query()] = None,
) -> ConversationListResponse:
    user = await _user(
        session,
        authenticated_user=authenticated_user,
        phone_number=phone_number,
        settings=settings,
    )
    await resolve_direct_conversation(session, user_id=user.id)
    await session.commit()
    conversations = await list_user_conversations(session, user_id=user.id)
    return ConversationListResponse(
        conversations=[
            await _conversation_response(session, conversation) for conversation in conversations
        ]
    )


@router.post("/groups", response_model=ConversationResponse)
async def create_group(
    request: GroupCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
) -> ConversationResponse:
    user = await _user(
        session,
        authenticated_user=authenticated_user,
        phone_number=request.phone_number,
        settings=settings,
    )
    conversation = await create_web_group(session, owner=user, title=request.title)
    return await _conversation_response(session, conversation)


@router.patch("/groups/{conversation_id}", response_model=ConversationResponse)
async def update_group(
    conversation_id: UUID,
    request: GroupUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
) -> ConversationResponse:
    user = await _user(
        session,
        authenticated_user=authenticated_user,
        phone_number=request.phone_number,
        settings=settings,
    )
    try:
        conversation = await rename_group(
            session,
            conversation_id=conversation_id,
            user_id=user.id,
            title=request.title,
        )
    except GroupNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except GroupPermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return await _conversation_response(session, conversation)


@router.post("/groups/{conversation_id}/invites", response_model=GroupInviteResponse)
async def invite_to_group(
    conversation_id: UUID,
    request: GroupAction,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
) -> GroupInviteResponse:
    user = await _user(
        session,
        authenticated_user=authenticated_user,
        phone_number=request.phone_number,
        settings=settings,
    )
    try:
        result = await create_group_invite(
            session,
            conversation_id=conversation_id,
            user_id=user.id,
        )
    except GroupNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except GroupPermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    base_url = settings.web_app_url.rstrip("/")
    return GroupInviteResponse(
        invite_url=f"{base_url}/?group_invite={quote(result.token)}",
        expires_at=result.invite.expires_at,
    )


@router.post("/groups/join", response_model=ConversationResponse)
async def join_group(
    request: GroupJoin,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
) -> ConversationResponse:
    user = await _user(
        session,
        authenticated_user=authenticated_user,
        phone_number=request.phone_number,
        settings=settings,
    )
    try:
        conversation = await join_group_from_invite(
            session,
            token=request.token,
            user=user,
        )
    except GroupInviteError as error:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=str(error),
        ) from error
    return await _conversation_response(session, conversation)


@router.post("/groups/{conversation_id}/leave", status_code=204)
async def leave_group(
    conversation_id: UUID,
    request: GroupAction,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
) -> None:
    user = await _user(
        session,
        authenticated_user=authenticated_user,
        phone_number=request.phone_number,
        settings=settings,
    )
    try:
        await leave_web_group(
            session,
            conversation_id=conversation_id,
            user_id=user.id,
        )
    except GroupNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except GroupPermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
