from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.api.dependencies import get_optional_authenticated_user, resolve_client_user
from benji_api.config import Settings, get_settings
from benji_api.db.session import get_session
from benji_api.models.generated_app import GeneratedApp
from benji_api.models.user import User
from benji_api.schemas.phone import PhoneNumber
from benji_api.services.generated_apps import (
    GeneratedAppBundle,
    GeneratedAppNotFoundError,
    GeneratedAppValidationError,
    create_generated_app_record,
    delete_generated_app_record,
    generated_app_url,
    get_generated_app_by_public_id,
    list_generated_apps,
    update_generated_app_record,
)

router = APIRouter(prefix="/apps", tags=["generated apps"])


class GeneratedAppCatalogRequest(BaseModel):
    phone_number: PhoneNumber | None = None


class GeneratedAppSummaryResponse(BaseModel):
    id: UUID
    public_id: str
    title: str
    description: str
    template: str
    theme: str
    access_mode: str
    app_url: str
    created_at: datetime
    updated_at: datetime


class GeneratedAppCatalogResponse(BaseModel):
    apps: list[GeneratedAppSummaryResponse]


class GeneratedAppRecordResponse(BaseModel):
    id: UUID
    module_id: str
    kind: str
    actor_name: str | None
    data: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class GeneratedAppResponse(GeneratedAppSummaryResponse):
    specification: dict[str, Any]
    records: list[GeneratedAppRecordResponse]


class GeneratedAppRecordCreateRequest(BaseModel):
    module_id: str | None = Field(default=None, min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=64)
    data: dict[str, Any]
    actor_name: str | None = Field(default=None, max_length=120)


class GeneratedAppRecordUpdateRequest(BaseModel):
    data: dict[str, Any]


@router.post("/catalog", response_model=GeneratedAppCatalogResponse)
async def generated_app_catalog(
    request: GeneratedAppCatalogRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authenticated_user: Annotated[User | None, Depends(get_optional_authenticated_user)],
) -> GeneratedAppCatalogResponse:
    user = await resolve_client_user(
        session,
        authenticated_user=authenticated_user,
        phone_number=request.phone_number,
        settings=settings,
    )
    apps = await list_generated_apps(session, user_id=user.id)
    return GeneratedAppCatalogResponse(apps=[_summary(app, settings=settings) for app in apps])


@router.get("/public/{public_id}", response_model=GeneratedAppResponse)
async def public_generated_app(
    public_id: Annotated[str, Path(min_length=20, max_length=64)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GeneratedAppResponse:
    return _bundle_response(
        await _get_bundle(session, public_id=public_id),
        settings=settings,
    )


@router.post("/public/{public_id}/records", response_model=GeneratedAppResponse)
async def add_generated_app_record(
    public_id: Annotated[str, Path(min_length=20, max_length=64)],
    request: GeneratedAppRecordCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GeneratedAppResponse:
    try:
        bundle = await create_generated_app_record(
            session,
            public_id=public_id,
            module_id=request.module_id,
            kind=request.kind,
            data=request.data,
            actor_name=request.actor_name,
        )
    except GeneratedAppNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except GeneratedAppValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    return _bundle_response(bundle, settings=settings)


@router.patch(
    "/public/{public_id}/records/{record_id}",
    response_model=GeneratedAppResponse,
)
async def edit_generated_app_record(
    public_id: Annotated[str, Path(min_length=20, max_length=64)],
    record_id: UUID,
    request: GeneratedAppRecordUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GeneratedAppResponse:
    try:
        bundle = await update_generated_app_record(
            session,
            public_id=public_id,
            record_id=record_id,
            data=request.data,
        )
    except GeneratedAppNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except GeneratedAppValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    return _bundle_response(bundle, settings=settings)


@router.delete(
    "/public/{public_id}/records/{record_id}",
    response_model=GeneratedAppResponse,
)
async def remove_generated_app_record(
    public_id: Annotated[str, Path(min_length=20, max_length=64)],
    record_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GeneratedAppResponse:
    try:
        bundle = await delete_generated_app_record(
            session,
            public_id=public_id,
            record_id=record_id,
        )
    except GeneratedAppNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except GeneratedAppValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    return _bundle_response(bundle, settings=settings)


async def _get_bundle(session: AsyncSession, *, public_id: str) -> GeneratedAppBundle:
    try:
        return await get_generated_app_by_public_id(session, public_id=public_id)
    except GeneratedAppNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


def _summary(app: GeneratedApp, *, settings: Settings) -> GeneratedAppSummaryResponse:
    return GeneratedAppSummaryResponse(
        id=app.id,
        public_id=app.public_id,
        title=app.title,
        description=app.description,
        template=app.template,
        theme=app.theme,
        access_mode=app.access_mode,
        app_url=generated_app_url(
            base_url=settings.generated_app_public_url,
            public_id=app.public_id,
        ),
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


def _bundle_response(
    bundle: GeneratedAppBundle,
    *,
    settings: Settings,
) -> GeneratedAppResponse:
    return GeneratedAppResponse(
        **_summary(bundle.app, settings=settings).model_dump(),
        specification=bundle.version.specification,
        records=[
            GeneratedAppRecordResponse(
                id=record.id,
                module_id=record.module_id,
                kind=record.kind,
                actor_name=record.actor_name,
                data=record.data,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            for record in bundle.records
        ],
    )
