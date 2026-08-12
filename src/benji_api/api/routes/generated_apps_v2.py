import re
from contextlib import suppress
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.api.dependencies import get_optional_authenticated_user
from benji_api.db.session import get_session
from benji_api.generated_app_contract import DOT_REMINDER_CREATE_CAPABILITY
from benji_api.models.generated_app_v2 import GeneratedAppRuntimeKind
from benji_api.models.user import User
from benji_api.services.generated_apps_v2 import (
    AppActor,
    CodeAppAuthorizationError,
    CodeAppConflictError,
    CodeAppNotFoundError,
    CodeAppRateLimitError,
    CodeAppValidationError,
    authorize_session,
    authorize_user,
    create_app_reminder,
    create_data_record,
    delete_data_record,
    get_runtime_bootstrap,
    list_data_records,
    redeem_access_ticket,
    update_data_record,
)

router = APIRouter(prefix="/apps/v2", tags=["generated apps v2"])


class AppResponse(BaseModel):
    id: UUID
    public_id: str
    title: str
    description: str
    status: str
    access_mode: str
    updated_at: datetime


class RevisionResponse(BaseModel):
    id: UUID
    revision_number: int
    manifest: dict[str, Any]
    artifact: dict[str, Any]
    artifact_url: str
    artifact_sha256: str
    sdk_version: str


def _runtime_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Return only immutable assets needed by the browser, not source/build telemetry."""
    return {
        "render_document": artifact.get("render_document", {}),
        "browser_bundle": artifact.get("browser_bundle"),
    }


class BuildResponse(BaseModel):
    id: UUID
    status: str
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class RuntimeBootstrapResponse(BaseModel):
    runtime_kind: Literal["legacy", "code"]
    app: AppResponse
    active_revision: RevisionResponse | None
    latest_build: BuildResponse | None
    access: dict[str, Any]


class RedeemRequest(BaseModel):
    ticket: str = Field(min_length=32, max_length=256)


class RedeemResponse(BaseModel):
    session_token: str
    expires_at: datetime
    role: str


class RuntimeActionRequest(BaseModel):
    operation: Literal[
        "app.data.get",
        "records.list",
        "records.create",
        "records.update",
        "records.delete",
        "dot.reminder.create",
    ]
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class RuntimeActionResponse(BaseModel):
    operation: str
    data: Any
    meta: dict[str, Any] = Field(default_factory=dict)


@router.get("/{public_id}", response_model=RuntimeBootstrapResponse)
async def code_app_bootstrap(
    public_id: Annotated[str, Path(min_length=20, max_length=64)],
    session: Annotated[AsyncSession, Depends(get_session)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
    app_session_token: Annotated[str | None, Header(alias="X-Dot-App-Session")] = None,
) -> RuntimeBootstrapResponse:
    try:
        bootstrap = await get_runtime_bootstrap(session, public_id=public_id)
    except CodeAppNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    app = bootstrap.app
    revision = bootstrap.revision
    build = bootstrap.build
    actor: AppActor | None = None
    with suppress(CodeAppAuthorizationError, CodeAppNotFoundError):
        actor = await _actor(
            session,
            app_id=app.id,
            authenticated_user=authenticated_user,
            app_session_token=app_session_token,
        )
    return RuntimeBootstrapResponse(
        runtime_kind=(
            "code"
            if app.runtime_kind == GeneratedAppRuntimeKind.CODE.value
            else "legacy"
        ),
        app=AppResponse(
            id=app.id,
            public_id=app.public_id,
            title=app.title,
            description=app.description,
            status=app.status,
            access_mode=app.access_mode,
            updated_at=app.updated_at,
        ),
        active_revision=(
            RevisionResponse(
                id=revision.id,
                revision_number=revision.revision_number,
                manifest=revision.manifest,
                artifact=_runtime_artifact(revision.artifact),
                artifact_url=revision.artifact_url,
                artifact_sha256=revision.artifact_sha256,
                sdk_version=revision.sdk_version,
            )
            if revision is not None and actor is not None
            else None
        ),
        latest_build=(
            BuildResponse(
                id=build.id,
                status=build.status,
                error=(
                    "This build could not be completed."
                    if build.status == "failed" and build.error
                    else None
                ),
                created_at=build.created_at,
                completed_at=build.completed_at,
            )
            if build is not None
            else None
        ),
        access={
            "mode": app.access_mode,
            "state": "authorized" if actor is not None else "ticket_required",
            "role": actor.role if actor is not None else None,
            "can_edit": actor is not None and actor.role in {"owner", "editor", "member"},
            "capabilities": (
                [DOT_REMINDER_CREATE_CAPABILITY]
                if (
                    actor is not None
                    and actor.identity_verified
                    and actor.user_id is not None
                    and actor.role in {"owner", "editor"}
                    and revision is not None
                    and DOT_REMINDER_CREATE_CAPABILITY
                    in revision.manifest.get("capabilities", [])
                )
                else []
            ),
        },
    )


@router.post("/{public_id}/sessions/redeem", response_model=RedeemResponse)
async def redeem_code_app_session(
    public_id: Annotated[str, Path(min_length=20, max_length=64)],
    request: RedeemRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RedeemResponse:
    try:
        raw, app_session = await redeem_access_ticket(
            session, public_id=public_id, token=request.ticket
        )
    except CodeAppNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except CodeAppAuthorizationError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error
    return RedeemResponse(
        session_token=raw,
        expires_at=app_session.expires_at,
        role=app_session.role,
    )


@router.post("/{app_id}/actions", response_model=RuntimeActionResponse)
async def invoke_code_app_action(
    app_id: UUID,
    request: RuntimeActionRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
    app_session_token: Annotated[str | None, Header(alias="X-Dot-App-Session")] = None,
) -> RuntimeActionResponse:
    try:
        actor = await _actor(
            session,
            app_id=app_id,
            authenticated_user=authenticated_user,
            app_session_token=app_session_token,
        )
        return await _dispatch_action(session, app_id=app_id, actor=actor, request=request)
    except CodeAppNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except CodeAppAuthorizationError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except CodeAppConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except CodeAppRateLimitError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(error)
        ) from error
    except CodeAppValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error


async def _actor(
    session: AsyncSession,
    *,
    app_id: UUID,
    authenticated_user: User | None,
    app_session_token: str | None,
) -> AppActor:
    user_authorization_error: CodeAppAuthorizationError | None = None
    if authenticated_user is not None:
        try:
            return await authorize_user(
                session,
                app_id=app_id,
                user_id=authenticated_user.id,
            )
        except CodeAppAuthorizationError as error:
            # A verified Dot identity is stronger than a bearer app session when it is
            # authorized for this app. If it belongs to someone else, the delivered
            # handoff can still grant its narrower app-session access.
            user_authorization_error = error
    if app_session_token:
        return await authorize_session(session, app_id=app_id, token=app_session_token)
    if user_authorization_error is not None:
        raise user_authorization_error
    raise CodeAppAuthorizationError("Sign in or open a valid private app link")


async def _dispatch_action(
    session: AsyncSession,
    *,
    app_id: UUID,
    actor: AppActor,
    request: RuntimeActionRequest,
) -> RuntimeActionResponse:
    args = request.args
    if request.operation == "app.data.get":
        if args:
            raise CodeAppValidationError("app.data.get does not accept arguments")
        return RuntimeActionResponse(
            operation=request.operation,
            data={"role": actor.role},
        )
    if request.operation == "records.list":
        records, total = await list_data_records(
            session,
            app_id=app_id,
            actor=actor,
            entity=_required_str(args, "entity"),
            limit=_integer(args.get("limit", 50), "limit"),
            offset=_integer(args.get("offset", 0), "offset"),
        )
        return RuntimeActionResponse(
            operation=request.operation,
            data=[_record(record) for record in records],
            meta={"total": total, "limit": min(max(int(args.get("limit", 50)), 1), 100)},
        )
    idempotency_key = request.idempotency_key
    if idempotency_key is None:
        raise CodeAppValidationError("Mutations require idempotency_key")
    if request.operation == "dot.reminder.create":
        _require_exact_args(
            args,
            {"title", "goal", "run_at", "timezone", "recurrence"},
        )
        task = await create_app_reminder(
            session,
            app_id=app_id,
            actor=actor,
            title=_required_str(args, "title"),
            goal=_required_str(args, "goal"),
            run_at=_required_aware_datetime(args, "run_at"),
            timezone=_required_str(args, "timezone"),
            recurrence=_required_str(args, "recurrence"),
            idempotency_key=idempotency_key,
        )
        return RuntimeActionResponse(
            operation=request.operation,
            data={
                "schedule_id": str(task.id),
                "title": task.title,
                "goal": task.payload.get("goal"),
                "run_at": task.scheduled_for.isoformat(),
                "timezone": task.timezone,
                "recurrence": task.recurrence,
                "delivery": task.delivery_provider or "dot_conversation",
            },
        )
    if request.operation == "records.create":
        record = await create_data_record(
            session,
            app_id=app_id,
            actor=actor,
            entity=_required_str(args, "entity"),
            data=_required_dict(args, "data"),
            idempotency_key=idempotency_key,
        )
        return RuntimeActionResponse(operation=request.operation, data=_record(record))
    record_id = _uuid(args, "record_id")
    expected_version = _integer(args.get("expected_version"), "expected_version")
    if request.operation == "records.update":
        record = await update_data_record(
            session,
            app_id=app_id,
            record_id=record_id,
            actor=actor,
            expected_version=expected_version,
            data=_required_dict(args, "data"),
            idempotency_key=idempotency_key,
        )
        return RuntimeActionResponse(operation=request.operation, data=_record(record))
    await delete_data_record(
        session,
        app_id=app_id,
        record_id=record_id,
        actor=actor,
        expected_version=expected_version,
        idempotency_key=idempotency_key,
    )
    return RuntimeActionResponse(operation=request.operation, data={"deleted": True})


def _record(record: Any) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "entity": record.entity,
        "data": record.data,
        "version": record.version,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _required_str(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise CodeAppValidationError(f"{name} is required")
    return value


def _required_dict(args: dict[str, Any], name: str) -> dict[str, Any]:
    value = args.get(name)
    if not isinstance(value, dict):
        raise CodeAppValidationError(f"{name} must be an object")
    return value


def _integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CodeAppValidationError(f"{name} must be an integer")
    return value


def _uuid(args: dict[str, Any], name: str) -> UUID:
    try:
        return UUID(_required_str(args, name))
    except ValueError as error:
        raise CodeAppValidationError(f"{name} must be a UUID") from error


def _required_aware_datetime(args: dict[str, Any], name: str) -> datetime:
    value = _required_str(args, name)
    if len(value) > 64 or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        raise CodeAppValidationError(f"{name} must be an RFC3339 timestamp with an offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CodeAppValidationError(f"{name} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CodeAppValidationError(f"{name} must include a timezone offset")
    return parsed


def _require_exact_args(args: dict[str, Any], expected: set[str]) -> None:
    if set(args) != expected:
        raise CodeAppValidationError("Reminder arguments do not match the supported contract")
