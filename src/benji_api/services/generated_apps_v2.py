from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.generated_app_contract import (
    DOT_REMINDER_CREATE_CAPABILITY,
    GeneratedAppCapabilityError,
    parse_generated_app_capabilities,
)
from benji_api.models.channel import (
    Conversation,
    ConversationKind,
    ConversationMember,
    ConversationMemberRole,
    ConversationMemberStatus,
)
from benji_api.models.generated_app import (
    GeneratedApp,
    GeneratedAppAccessMode,
    GeneratedAppStatus,
)
from benji_api.models.generated_app_v2 import (
    GeneratedAppAccessTicket,
    GeneratedAppBuildJob,
    GeneratedAppBuildStatus,
    GeneratedAppDataRecord,
    GeneratedAppDeployment,
    GeneratedAppEvent,
    GeneratedAppMembership,
    GeneratedAppRevision,
    GeneratedAppRevisionStatus,
    GeneratedAppRole,
    GeneratedAppRuntimeKind,
    GeneratedAppSession,
)
from benji_api.models.schedule import ScheduledTask, ScheduledTaskRecurrence
from benji_api.models.user import User, utc_now
from benji_api.services.channels import resolve_direct_conversation
from benji_api.services.schedules import (
    AGENT_REACHOUT_ACTION,
    ScheduleValidationError,
    create_scheduled_task,
    preferred_delivery_provider,
)
from benji_api.services.user_events import enqueue_user_event

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_ACCESS_TICKET_TTL_SECONDS = 7 * 86_400
_MAX_BUILD_REQUEST_BYTES = 1_000_000
_MAX_BUILD_ARTIFACT_BYTES = 4_000_000
_MAX_MANIFEST_BYTES = 64_000
_MAX_RECORD_BYTES = 16_000
_MAX_RECORDS_PER_APP = 10_000
_MAX_DATA_BYTES_PER_APP = 10_000_000
_MAX_MUTATIONS_PER_ACTOR_MINUTE = 120
_MAX_MUTATIONS_PER_APP_MINUTE = 600
_MAX_PRIVATE_TICKET_REDEMPTIONS = 12
_MAX_COLLABORATIVE_TICKET_REDEMPTIONS = 64
_MAX_ACTIVE_CODE_APPS_PER_USER = 20
_MAX_LIVE_BUILDS_PER_USER = 3
_MAX_REMINDER_HORIZON = timedelta(days=5 * 366)
_MAX_REVISION_CONTEXT_BYTES = 768_000
_MAX_SEED_DATA_BYTES = 128_000
_MAX_BUILD_ATTEMPTS = 3
_UNSET = object()
_WRITABLE_ROLES = {
    GeneratedAppRole.OWNER.value,
    GeneratedAppRole.EDITOR.value,
    GeneratedAppRole.MEMBER.value,
}


class CodeAppNotFoundError(LookupError):
    pass


class CodeAppAuthorizationError(PermissionError):
    pass


class CodeAppConflictError(RuntimeError):
    pass


class CodeAppStaleBuildError(CodeAppConflictError):
    pass


class CodeAppValidationError(ValueError):
    pass


class CodeAppRateLimitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AppActor:
    role: str
    user_id: UUID | None = None
    session_id: UUID | None = None
    identity_verified: bool = False


@dataclass(frozen=True, slots=True)
class ClaimedBuild:
    job_id: UUID
    app_id: UUID
    base_revision_id: UUID | None
    request: dict[str, Any]
    attempt: int


@dataclass(frozen=True, slots=True)
class RuntimeBootstrap:
    app: GeneratedApp
    revision: GeneratedAppRevision | None
    deployment: GeneratedAppDeployment | None
    build: GeneratedAppBuildJob | None


@dataclass(frozen=True, slots=True)
class OwnedCodeApp:
    """Owner-authorized app state safe to expose to Dot's private conversation tools."""

    app: GeneratedApp
    revision: GeneratedAppRevision | None
    deployment: GeneratedAppDeployment | None
    build: GeneratedAppBuildJob | None
    actor: AppActor


@dataclass(frozen=True, slots=True)
class StoredDataRecord:
    """Immutable response snapshot returned by an exact idempotent replay."""

    id: UUID
    entity: str
    data: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredScheduledTask:
    """Immutable reminder response returned even if the live schedule later changes."""

    id: UUID
    title: str
    payload: dict[str, Any]
    scheduled_for: datetime
    timezone: str
    recurrence: str
    delivery_provider: str | None


@dataclass(frozen=True, slots=True)
class StoredRollback:
    """Exact response snapshot for a retried deployment rollback."""

    app_id: UUID
    public_id: str
    title: str
    active_revision_id: UUID
    active_revision_number: int
    deployment_version: int
    rollback_is_reversible: bool


async def create_code_app_build(
    session: AsyncSession,
    *,
    user_id: UUID,
    conversation_id: UUID,
    requester_user_id: UUID | None = None,
    title: str,
    description: str,
    request: dict[str, Any],
    access_mode: str = GeneratedAppAccessMode.PRIVATE_LINK.value,
    delivery_provider: str | None = None,
    app_url: str | None = None,
    idempotency_key: str | None = None,
    app_base_url: str | None = None,
    idempotency_request_hash: str | None = None,
) -> tuple[GeneratedApp, GeneratedAppBuildJob, str]:
    requested_access_mode = _generated_app_access_mode(access_mode)
    clean_idempotency_key = (
        _text(idempotency_key, "idempotency_key", 160)
        if idempotency_key is not None
        else None
    )
    conversation = await _locked_owned_conversation(
        session,
        conversation_id=conversation_id,
        user_id=user_id,
        requester_user_id=requester_user_id or user_id,
    )
    is_group = conversation.kind == ConversationKind.GROUP.value
    effective_access_mode = (
        GeneratedAppAccessMode.COLLABORATIVE_LINK.value
        if is_group
        else requested_access_mode
    )
    request_hash = _validated_request_hash(idempotency_request_hash) or (
        _idempotency_request_hash(
            "app.create",
            {
                "user_id": str(user_id),
                "conversation_id": str(conversation_id),
                "requester_user_id": str(requester_user_id or user_id),
                "title": title,
                "description": description,
                "request": request,
                "access_mode": effective_access_mode,
                "delivery_provider": delivery_provider,
                "app_url": app_url,
                "app_base_url": app_base_url,
            },
        )
    )
    if clean_idempotency_key is not None:
        replay = await _build_by_idempotency_key(session, clean_idempotency_key)
        if replay is not None:
            return await _replay_created_build(
                session,
                job=replay,
                request_hash=request_hash,
            )
    clean_title = _text(title, "title", 120)
    clean_description = _optional_text(description, 500)
    if not isinstance(request, dict) or not request:
        raise CodeAppValidationError("Build request must be a non-empty object")
    _require_json_size(request, "Build request", _MAX_BUILD_REQUEST_BYTES)
    await _enforce_user_app_build_quota(session, user_id=user_id, creating_app=True)
    app = GeneratedApp(
        user_id=user_id,
        conversation_id=conversation_id,
        public_id=secrets.token_urlsafe(24),
        title=clean_title,
        description=clean_description,
        template="code_app",
        theme="dot",
        access_mode=effective_access_mode,
        runtime_kind=GeneratedAppRuntimeKind.CODE.value,
        current_version=0,
    )
    session.add(app)
    await session.flush()
    session.add(
        GeneratedAppMembership(
            app_id=app.id,
            user_id=user_id,
            role=GeneratedAppRole.OWNER.value,
        )
    )
    job = GeneratedAppBuildJob(
        app_id=app.id,
        request=request,
        delivery_provider=delivery_provider,
        app_url=app_url or f"/a/{app.public_id}",
        idempotency_key=clean_idempotency_key,
        request_hash=request_hash if clean_idempotency_key is not None else None,
    )
    session.add(job)
    await session.flush()
    ticket = await issue_access_ticket(
        session,
        app_id=app.id,
        issuer_user_id=user_id,
        principal_user_id=(
            None
            if effective_access_mode == GeneratedAppAccessMode.COLLABORATIVE_LINK.value
            else user_id
        ),
        role=(
            GeneratedAppRole.MEMBER.value
            if effective_access_mode == GeneratedAppAccessMode.COLLABORATIVE_LINK.value
            else GeneratedAppRole.OWNER.value
        ),
        ttl_seconds=_MAX_ACCESS_TICKET_TTL_SECONDS,
    )
    if app_base_url is not None:
        job.app_url = f"{app_base_url.rstrip('/')}/a/{app.public_id}#handoff={ticket}"
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if clean_idempotency_key is None:
            raise
        replay = await _build_by_idempotency_key(session, clean_idempotency_key)
        if replay is None:
            raise
        return await _replay_created_build(
            session,
            job=replay,
            request_hash=request_hash,
        )
    return app, job, ticket


async def _seed_code_app_records(
    session: AsyncSession,
    *,
    app_id: UUID,
    user_id: UUID,
    manifest: dict[str, Any],
    seed_data: dict[str, Any],
) -> None:
    """Persist validated seed lists once, without replacing user-created records."""

    _validate_manifest(manifest)
    for definition in manifest.get("entities", []):
        entity = definition["name"]
        candidates = seed_data.get(entity)
        if candidates is None:
            candidates = seed_data.get(f"{entity}s")
        if not isinstance(candidates, list):
            continue
        for raw in candidates[:200]:
            if not isinstance(raw, dict):
                continue
            clean = _validate_entity_data(manifest, entity, raw)
            data_bytes = _json_bytes(clean)
            await _enforce_app_storage_limit(
                session,
                app_id=app_id,
                added_bytes=data_bytes,
            )
            session.add(
                GeneratedAppDataRecord(
                    app_id=app_id,
                    entity=entity,
                    data=clean,
                    data_bytes=data_bytes,
                    created_by_user_id=user_id,
                    updated_by_user_id=user_id,
                )
            )


async def queue_code_app_revision(
    session: AsyncSession,
    *,
    user_id: UUID,
    app_id: UUID,
    request: dict[str, Any],
    delivery_provider: str | None = None,
    app_url: str | None = None,
    idempotency_key: str | None = None,
    idempotency_request_hash: str | None = None,
) -> GeneratedAppBuildJob:
    if not isinstance(request, dict) or not request:
        raise CodeAppValidationError("Build request must be a non-empty object")
    _require_json_size(request, "Build request", _MAX_BUILD_REQUEST_BYTES)
    clean_idempotency_key = (
        _text(idempotency_key, "idempotency_key", 160)
        if idempotency_key is not None
        else None
    )
    request_hash = _validated_request_hash(idempotency_request_hash) or (
        _idempotency_request_hash(
            "app.revise",
            {
                "user_id": str(user_id),
                "app_id": str(app_id),
                "request": request,
                "delivery_provider": delivery_provider,
                "app_url": app_url,
            },
        )
    )
    app = await _locked_code_app(session, app_id)
    actor = await _authorize_user_for_locked_app(
        session,
        app=app,
        user_id=user_id,
    )
    if actor.role not in {GeneratedAppRole.OWNER.value, GeneratedAppRole.EDITOR.value}:
        raise CodeAppAuthorizationError("Only an owner or editor can change this app")
    if clean_idempotency_key is not None:
        replay = await _build_by_idempotency_key(session, clean_idempotency_key)
        if replay is not None:
            _validate_build_replay(replay, request_hash=request_hash)
            return replay
    await _ensure_no_live_build(session, app_id=app_id)
    await _enforce_user_app_build_quota(session, user_id=user_id, creating_app=False)
    deployment = await session.get(GeneratedAppDeployment, app_id)
    job = GeneratedAppBuildJob(
        app_id=app_id,
        base_revision_id=deployment.active_revision_id if deployment else None,
        request=request,
        delivery_provider=delivery_provider,
        app_url=app_url or f"/a/{app.public_id}",
        idempotency_key=clean_idempotency_key,
        request_hash=request_hash if clean_idempotency_key is not None else None,
    )
    session.add(job)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        if clean_idempotency_key is not None:
            replay = await _build_by_idempotency_key(session, clean_idempotency_key)
            if replay is not None:
                _validate_build_replay(replay, request_hash=request_hash)
                return replay
        raise CodeAppConflictError(
            "This app already has a revision in progress; wait for it to finish"
        ) from error
    return job


async def get_owned_code_app(
    session: AsyncSession,
    *,
    app_id: UUID,
    user_id: UUID,
) -> OwnedCodeApp:
    """Return a code app only when the requesting user is its canonical owner."""

    app = await _locked_code_app(session, app_id)
    actor = await _authorize_user_for_locked_app(
        session,
        app=app,
        user_id=user_id,
    )
    if actor.role != GeneratedAppRole.OWNER.value:
        raise CodeAppAuthorizationError("Only the app owner can manage it through private chat")
    bootstrap = await _runtime_bootstrap_for_app(session, app)
    return OwnedCodeApp(
        app=bootstrap.app,
        revision=bootstrap.revision,
        deployment=bootstrap.deployment,
        build=bootstrap.build,
        actor=actor,
    )


async def queue_owned_code_app_revision(
    session: AsyncSession,
    *,
    user_id: UUID,
    app_id: UUID,
    revision_request: str,
    title: str | None = None,
    description: str | None = None,
    visual_direction: str | None = None,
    manifest: dict[str, Any] | None = None,
    seed_data: dict[str, Any] | None = None,
    delivery_provider: str | None = None,
    app_url: str | None = None,
    idempotency_key: str | None = None,
    idempotency_request_hash: str | None = None,
) -> GeneratedAppBuildJob:
    """Queue a complete revision blueprint while preserving the deployed app by default."""

    clean_idempotency_key = (
        _text(idempotency_key, "idempotency_key", 160)
        if idempotency_key is not None
        else None
    )
    request_hash = _validated_request_hash(idempotency_request_hash)
    owned = await get_owned_code_app(session, app_id=app_id, user_id=user_id)
    if clean_idempotency_key is not None and request_hash is not None:
        replay = await _build_by_idempotency_key(session, clean_idempotency_key)
        if replay is not None:
            _validate_build_replay(replay, request_hash=request_hash)
            return replay
    if owned.revision is None or owned.deployment is None:
        raise CodeAppConflictError("App must finish its first build before it can be revised")
    change = _text(revision_request, "revision_request", 4_000)
    previous_blueprint = await _active_blueprint(
        session,
        app_id=app_id,
        revision_id=owned.revision.id,
    )
    intended_title = _text(title, "title", 120) if title is not None else owned.app.title
    intended_description = (
        _optional_text(description, 500) if description is not None else owned.app.description
    )
    intended_manifest = manifest if manifest is not None else dict(owned.revision.manifest)
    _validate_manifest(intended_manifest)
    if manifest is not None:
        await _validate_existing_records_for_manifest(
            session,
            app_id=app_id,
            manifest=intended_manifest,
        )
    intended_seed_data = (
        seed_data
        if seed_data is not None
        else _json_object(previous_blueprint.get("seed_data"), default={})
    )
    _require_json_size(intended_seed_data, "Seed data", _MAX_SEED_DATA_BYTES)
    blueprint = {
        "title": intended_title,
        "description": intended_description,
        "purpose": _fallback_text(
            previous_blueprint.get("purpose"),
            intended_description or intended_title,
            500,
        ),
        "layout": _fallback_text(previous_blueprint.get("layout"), "workspace", 48),
        "accent": _fallback_text(previous_blueprint.get("accent"), "coral", 16),
        "product_brief": _fallback_text(
            previous_blueprint.get("product_brief"),
            intended_description or intended_title,
            4_000,
        ),
        "visual_direction": (
            _text(visual_direction, "visual_direction", 1_000)
            if visual_direction is not None
            else _fallback_text(
                previous_blueprint.get("visual_direction"),
                "purpose-native and mobile-first",
                1_000,
            )
        ),
        "manifest": intended_manifest,
        "seed_data": intended_seed_data,
        "revision_request": change,
        "base_revision": _revision_context(owned.revision),
    }
    return await queue_code_app_revision(
        session,
        user_id=user_id,
        app_id=app_id,
        request={
            "blueprint": blueprint,
            "app_metadata": {
                "title": intended_title,
                "description": intended_description,
            },
        },
        delivery_provider=delivery_provider,
        app_url=app_url,
        idempotency_key=clean_idempotency_key,
        idempotency_request_hash=request_hash,
    )


async def rollback_owned_code_app(
    session: AsyncSession,
    *,
    user_id: UUID,
    app_id: UUID,
    expected_active_revision_id: UUID,
    idempotency_key: str | None = None,
) -> OwnedCodeApp | StoredRollback:
    """Atomically swap an owner's app back to its previous deployed revision.

    ``promote_revision`` moves the revision being replaced into the previous slot, so
    invoking this operation again reverses the rollback without changing the public app URL.
    """

    app = await _locked_code_app(session, app_id)
    actor = await _authorize_user_for_locked_app(
        session,
        app=app,
        user_id=user_id,
    )
    if actor.role != GeneratedAppRole.OWNER.value or app.user_id != user_id:
        raise CodeAppAuthorizationError("Only the app owner can roll back this app")
    clean_idempotency_key = (
        _text(idempotency_key, "idempotency_key", 160)
        if idempotency_key is not None
        else None
    )
    request_hash = _idempotency_request_hash(
        "deployment.rollback",
        {"expected_active_revision_id": str(expected_active_revision_id)},
    )
    if clean_idempotency_key is not None:
        existing = await _event_by_key(session, app_id, clean_idempotency_key)
        if existing is not None:
            return _stored_rollback_from_event(
                existing,
                app=app,
                request_hash=request_hash,
            )
    deployment_statement = select(GeneratedAppDeployment).where(
        GeneratedAppDeployment.app_id == app_id
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        deployment_statement = deployment_statement.with_for_update()
    deployment = await session.scalar(
        deployment_statement.execution_options(populate_existing=True)
    )
    if deployment is None or deployment.previous_revision_id is None:
        raise CodeAppConflictError("This app does not have a previous deployed version")
    if deployment.active_revision_id != expected_active_revision_id:
        raise CodeAppConflictError(
            "The deployed app changed since it was inspected; inspect it again before rollback"
        )

    pending_build = await session.scalar(
        select(GeneratedAppBuildJob)
        .where(
            GeneratedAppBuildJob.app_id == app_id,
            GeneratedAppBuildJob.status.in_(
                {
                    GeneratedAppBuildStatus.QUEUED.value,
                    GeneratedAppBuildStatus.CLAIMED.value,
                }
            ),
        )
        .order_by(GeneratedAppBuildJob.created_at.desc())
        .limit(1)
    )
    if pending_build is not None:
        raise CodeAppConflictError(
            "This app has a revision in progress; wait for it to finish before rolling back"
        )

    replaced_revision = await session.get(GeneratedAppRevision, deployment.active_revision_id)
    target_revision = await session.get(GeneratedAppRevision, deployment.previous_revision_id)
    if (
        replaced_revision is None
        or target_revision is None
        or target_revision.app_id != app_id
        or target_revision.status != GeneratedAppRevisionStatus.READY.value
    ):
        raise CodeAppConflictError("The previous deployed version is not available")

    # Data remains live across revisions. Re-check under the same app/deployment locks used for
    # the pointer swap so a rollback can never activate a schema that rejects current records.
    await _validate_existing_records_for_manifest(
        session,
        app_id=app_id,
        manifest=target_revision.manifest,
    )

    deployment = await promote_revision(
        session,
        app_id=app_id,
        revision_id=target_revision.id,
        commit=False,
    )
    rollback_response = {
        "app_id": str(app.id),
        "public_id": app.public_id,
        "title": app.title,
        "active_revision_id": str(target_revision.id),
        "active_revision_number": target_revision.revision_number,
        "deployment_version": deployment.deployment_version,
        "rollback_is_reversible": deployment.previous_revision_id is not None,
    }
    session.add(
        GeneratedAppEvent(
            app_id=app_id,
            event_type="app.deployment.rolled_back",
            actor_user_id=user_id,
            idempotency_key=(
                clean_idempotency_key
                or f"app-rollback:{deployment.deployment_version}"
            ),
            operation=("deployment.rollback" if clean_idempotency_key is not None else None),
            request_hash=(request_hash if clean_idempotency_key is not None else None),
            response=(rollback_response if clean_idempotency_key is not None else {}),
            payload={
                "from_revision_id": str(replaced_revision.id),
                "from_revision_number": replaced_revision.revision_number,
                "to_revision_id": str(target_revision.id),
                "to_revision_number": target_revision.revision_number,
                "deployment_version": deployment.deployment_version,
            },
        )
    )
    await session.commit()
    return OwnedCodeApp(
        app=app,
        revision=target_revision,
        deployment=deployment,
        build=await session.scalar(
            select(GeneratedAppBuildJob)
            .where(GeneratedAppBuildJob.app_id == app_id)
            .order_by(GeneratedAppBuildJob.created_at.desc())
            .limit(1)
        ),
        actor=actor,
    )


async def claim_next_build(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int = 120,
) -> ClaimedBuild | None:
    clean_worker_id = _text(worker_id, "worker_id", 120)
    while True:
        now = datetime.now(UTC)
        eligible = or_(
            GeneratedAppBuildJob.status == GeneratedAppBuildStatus.QUEUED.value,
            and_(
                GeneratedAppBuildJob.status == GeneratedAppBuildStatus.CLAIMED.value,
                GeneratedAppBuildJob.lease_expires_at < now,
            ),
        )
        statement = (
            select(GeneratedAppBuildJob)
            .where(eligible)
            .order_by(GeneratedAppBuildJob.created_at)
            .limit(1)
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        job = await session.scalar(statement.execution_options(populate_existing=True))
        if job is None:
            return None
        if job.attempts >= _MAX_BUILD_ATTEMPTS:
            await _terminalize_exhausted_build_claim(session, job=job, now=now)
            await session.commit()
            # Do not make the poller wait another cycle: after atomically settling the crashed
            # job, continue directly to the next eligible build.
            continue
        job.status = GeneratedAppBuildStatus.CLAIMED.value
        job.claimed_by = clean_worker_id
        job.attempts += 1
        job.started_at = job.started_at or now
        job.lease_expires_at = now + timedelta(seconds=max(15, min(lease_seconds, 900)))
        job.updated_at = now
        await session.commit()
        return ClaimedBuild(
            job_id=job.id,
            app_id=job.app_id,
            base_revision_id=job.base_revision_id,
            request=dict(job.request),
            attempt=job.attempts,
        )


async def _terminalize_exhausted_build_claim(
    session: AsyncSession,
    *,
    job: GeneratedAppBuildJob,
    now: datetime,
) -> None:
    """Fail one repeatedly abandoned lease in the same transaction as its outbox event."""

    failure_code = "build_worker_lease_exhausted"
    job.status = GeneratedAppBuildStatus.FAILED.value
    job.claimed_by = None
    job.lease_expires_at = None
    job.completed_at = now
    job.updated_at = now
    job.error = json.dumps(
        {
            "code": failure_code,
            "message": "The app builder stopped before settling this build too many times",
            "retryable": False,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    job.result = {
        "build_metadata": {"abandoned_claims": job.attempts},
        "retryable": False,
        "requeued": False,
        "failure_code": failure_code,
    }
    app = await session.get(GeneratedApp, job.app_id)
    if app is None:  # pragma: no cover - the app foreign key normally cascades the job.
        return
    await enqueue_user_event(
        session,
        user_id=app.user_id,
        conversation_id=app.conversation_id,
        event_type="app.build.failed",
        source="generated_app_builder",
        idempotency_key=f"app-build-failed:{job.id}",
        delivery_provider=job.delivery_provider,
        payload={
            "app_id": str(app.id),
            "public_id": app.public_id,
            "title": app.title,
            "app_url": job.app_url,
            "description": app.description,
            "purpose": _build_purpose(job.request, app.description),
            "build_job_id": str(job.id),
            "duration_ms": _duration_ms(job.created_at, now),
            "failure_code": failure_code,
            "retryable": False,
        },
    )


async def complete_build(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_attempt: int,
    manifest: dict[str, Any],
    source_files: dict[str, str],
    artifact_url: str,
    artifact_sha256: str,
    sdk_version: str,
    dependency_lock: dict[str, str] | None = None,
    test_results: dict[str, Any] | None = None,
    artifact: dict[str, Any] | None = None,
    app_url: str | None = None,
    handoff_base_url: str | None = None,
    build_metadata: dict[str, Any] | None = None,
) -> GeneratedAppRevision:
    job = await _claimed_job(
        session,
        job_id=job_id,
        worker_id=worker_id,
        expected_attempt=expected_attempt,
    )
    _validate_manifest(manifest)
    # Compiled React plus the fixed runtime can legitimately exceed 1 MB. Keep the
    # database-backed MVP bounded while leaving room beneath the compiler's 3.5 MB cap.
    _require_json_size(artifact or {}, "Build artifact", _MAX_BUILD_ARTIFACT_BYTES)
    _require_json_size(dependency_lock or {}, "Dependency lock", 64_000)
    _require_json_size(test_results or {}, "Test results", 128_000)
    if not source_files or any(not isinstance(key, str) for key in source_files):
        raise CodeAppValidationError("A revision requires generated source files")
    if not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
        raise CodeAppValidationError("artifact_sha256 must be a lowercase SHA-256 digest")
    # Serializing on the stable app row prevents two successful workers assigning the same
    # immutable revision number under PostgreSQL.
    app = await _locked_code_app(session, job.app_id)
    next_number = (
        await session.scalar(
            select(func.max(GeneratedAppRevision.revision_number)).where(
                GeneratedAppRevision.app_id == job.app_id
            )
        )
        or 0
    ) + 1
    blueprint = job.request.get("blueprint")
    if not isinstance(blueprint, dict):
        blueprint = {}
    metadata = job.request.get("app_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    revision_title = _text(
        metadata.get("title") or blueprint.get("title") or app.title,
        "title",
        120,
    )
    revision_description = _optional_text(
        metadata.get("description")
        if metadata.get("description") is not None
        else blueprint.get("description", app.description),
        500,
    )
    revision_seed_data = _json_object(blueprint.get("seed_data"), default={})
    _require_json_size(revision_seed_data, "Seed data", _MAX_SEED_DATA_BYTES)
    revision = GeneratedAppRevision(
        app_id=job.app_id,
        revision_number=next_number,
        title=revision_title,
        description=revision_description,
        manifest=manifest,
        seed_data=revision_seed_data,
        source_files=source_files,
        artifact=artifact or {},
        artifact_url=_text(artifact_url, "artifact_url", 4000),
        artifact_sha256=artifact_sha256,
        sdk_version=_text(sdk_version, "sdk_version", 64),
        dependency_lock=dependency_lock or {},
        test_results=test_results or {},
    )
    session.add(revision)
    await session.flush()
    # A user may have written data after this revision was queued. The stable app-row lock held
    # above serializes this final compatibility check with record mutations in PostgreSQL.
    await _validate_existing_records_for_manifest(
        session,
        app_id=job.app_id,
        manifest=manifest,
    )
    await promote_revision(
        session,
        app_id=job.app_id,
        revision_id=revision.id,
        expected_active_revision_id=job.base_revision_id,
        commit=False,
    )
    now = datetime.now(UTC)
    job.result_revision_id = revision.id
    job.status = GeneratedAppBuildStatus.SUCCEEDED.value
    job.error = None
    job.result = {
        "revision_id": str(revision.id),
        "artifact_sha256": artifact_sha256,
        "build_metadata": build_metadata or {},
    }
    job.completed_at = now
    job.lease_expires_at = None
    job.updated_at = now
    app.title = revision.title
    app.description = revision.description
    completion_url = app_url or job.app_url
    if handoff_base_url is not None:
        conversation = await session.get(Conversation, app.conversation_id)
        is_group = conversation is not None and conversation.kind == ConversationKind.GROUP.value
        if is_group:
            ticket = await _issue_trusted_group_handoff_ticket(
                session,
                app=app,
                conversation=conversation,
                ttl_seconds=_MAX_ACCESS_TICKET_TTL_SECONDS,
            )
        else:
            ticket = await issue_access_ticket(
                session,
                app_id=app.id,
                issuer_user_id=app.user_id,
                principal_user_id=(
                    None
                    if app.access_mode == GeneratedAppAccessMode.COLLABORATIVE_LINK.value
                    else app.user_id
                ),
                role=(
                    GeneratedAppRole.MEMBER.value
                    if app.access_mode == GeneratedAppAccessMode.COLLABORATIVE_LINK.value
                    else GeneratedAppRole.OWNER.value
                ),
                ttl_seconds=_MAX_ACCESS_TICKET_TTL_SECONDS,
            )
        completion_url = f"{_text(handoff_base_url, 'handoff_base_url', 4000)}#handoff={ticket}"
        job.app_url = completion_url
    duration_ms = _duration_ms(job.created_at, now)
    await enqueue_user_event(
        session,
        user_id=app.user_id,
        conversation_id=app.conversation_id,
        event_type="app.build.completed",
        source="generated_app_builder",
        idempotency_key=f"app-build-completed:{job.id}",
        delivery_provider=job.delivery_provider,
        payload={
            "app_id": str(app.id),
            "public_id": app.public_id,
            "title": app.title,
            "app_url": completion_url,
            "description": app.description,
            "purpose": _build_purpose(job.request, app.description),
            "revision_id": str(revision.id),
            "build_job_id": str(job.id),
            "duration_ms": duration_ms,
            **_safe_build_telemetry(artifact, test_results, build_metadata),
        },
    )
    await session.commit()
    return revision


async def fail_build(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_attempt: int,
    error: str,
    app_url: str | None = None,
    build_metadata: dict[str, Any] | None = None,
    retryable: bool = False,
) -> GeneratedAppBuildJob:
    job = await _claimed_job(
        session,
        job_id=job_id,
        worker_id=worker_id,
        expected_attempt=expected_attempt,
    )
    now = datetime.now(UTC)
    job.error = _text(error, "error", 4000)
    job.lease_expires_at = None
    job.updated_at = now
    app = await session.get(GeneratedApp, job.app_id)
    if app is None:
        raise CodeAppNotFoundError("App was not found")
    failure_code, parsed_retryable = _safe_failure(error)
    safe_retryable = retryable if parsed_retryable is None else parsed_retryable
    should_retry = safe_retryable and job.attempts < _MAX_BUILD_ATTEMPTS
    job.result = {
        "build_metadata": build_metadata or {},
        "retryable": safe_retryable,
        "requeued": should_retry,
        "failure_code": failure_code,
    }
    if should_retry:
        job.status = GeneratedAppBuildStatus.QUEUED.value
        job.claimed_by = None
        job.completed_at = None
        await session.commit()
        return job
    job.status = GeneratedAppBuildStatus.FAILED.value
    job.completed_at = now
    await enqueue_user_event(
        session,
        user_id=app.user_id,
        conversation_id=app.conversation_id,
        event_type="app.build.failed",
        source="generated_app_builder",
        idempotency_key=f"app-build-failed:{job.id}",
        delivery_provider=job.delivery_provider,
        payload={
            "app_id": str(app.id),
            "public_id": app.public_id,
            "title": app.title,
            "app_url": app_url or job.app_url,
            "description": app.description,
            "purpose": _build_purpose(job.request, app.description),
            "build_job_id": str(job.id),
            "duration_ms": _duration_ms(job.created_at, now),
            "failure_code": failure_code,
            "retryable": safe_retryable,
        },
    )
    await session.commit()
    return job


async def promote_revision(
    session: AsyncSession,
    *,
    app_id: UUID,
    revision_id: UUID,
    expected_active_revision_id: UUID | None | object = _UNSET,
    commit: bool = True,
) -> GeneratedAppDeployment:
    revision = await session.get(GeneratedAppRevision, revision_id)
    if revision is None or revision.app_id != app_id:
        raise CodeAppNotFoundError("Revision was not found")
    app = await session.get(GeneratedApp, app_id)
    if app is None or app.runtime_kind != GeneratedAppRuntimeKind.CODE.value:
        raise CodeAppNotFoundError("Code app was not found")
    deployment_statement = select(GeneratedAppDeployment).where(
        GeneratedAppDeployment.app_id == app_id
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        deployment_statement = deployment_statement.with_for_update()
    deployment = await session.scalar(
        deployment_statement.execution_options(populate_existing=True)
    )
    if expected_active_revision_id is not _UNSET:
        current_revision_id = deployment.active_revision_id if deployment is not None else None
        if current_revision_id != expected_active_revision_id:
            raise CodeAppStaleBuildError(
                "The app changed after this build started; the stale build was not deployed"
            )
    is_first_deployment = deployment is None
    if deployment is None:
        deployment = GeneratedAppDeployment(app_id=app_id, active_revision_id=revision_id)
        session.add(deployment)
    elif deployment.active_revision_id != revision_id:
        if expected_active_revision_id is _UNSET:
            deployment.previous_revision_id = deployment.active_revision_id
            deployment.active_revision_id = revision_id
            deployment.deployment_version += 1
            deployment.deployed_at = utc_now()
        else:
            promoted = await session.scalar(
                update(GeneratedAppDeployment)
                .where(
                    GeneratedAppDeployment.app_id == app_id,
                    GeneratedAppDeployment.active_revision_id == expected_active_revision_id,
                )
                .values(
                    previous_revision_id=GeneratedAppDeployment.active_revision_id,
                    active_revision_id=revision_id,
                    deployment_version=GeneratedAppDeployment.deployment_version + 1,
                    deployed_at=utc_now(),
                )
                .returning(GeneratedAppDeployment.app_id)
            )
            if promoted is None:
                raise CodeAppStaleBuildError(
                    "The app changed while this build was deploying; "
                    "the stale build was not deployed"
                )
            deployment = await session.scalar(
                select(GeneratedAppDeployment)
                .where(GeneratedAppDeployment.app_id == app_id)
                .execution_options(populate_existing=True)
            )
            if deployment is None:  # pragma: no cover - protected by the successful CAS.
                raise CodeAppConflictError("App deployment disappeared during promotion")
    app.current_version = revision.revision_number
    app.title = revision.title
    app.description = revision.description
    if revision.seed_applied_at is None:
        existing_records = await session.scalar(
            select(func.count())
            .select_from(GeneratedAppDataRecord)
            .where(GeneratedAppDataRecord.app_id == app_id)
        )
        if is_first_deployment and int(existing_records or 0) == 0:
            await _seed_code_app_records(
                session,
                app_id=app_id,
                user_id=app.user_id,
                manifest=revision.manifest,
                seed_data=revision.seed_data,
            )
        # Mark even skipped seed sets as consumed. A later rollback or retry must never
        # resurrect generated starter data over a user's live app state.
        revision.seed_applied_at = utc_now()
    app.updated_at = utc_now()
    await session.flush()
    if commit:
        await session.commit()
    return deployment


async def get_runtime_bootstrap(session: AsyncSession, *, public_id: str) -> RuntimeBootstrap:
    app = await session.scalar(
        select(GeneratedApp).where(
            GeneratedApp.public_id == public_id,
            GeneratedApp.status == GeneratedAppStatus.ACTIVE.value,
        )
    )
    if app is None:
        raise CodeAppNotFoundError("App was not found")
    return await _runtime_bootstrap_for_app(session, app)


async def _runtime_bootstrap_for_app(
    session: AsyncSession,
    app: GeneratedApp,
) -> RuntimeBootstrap:
    deployment = await session.get(GeneratedAppDeployment, app.id)
    revision = (
        await session.get(GeneratedAppRevision, deployment.active_revision_id)
        if deployment is not None
        else None
    )
    build = await session.scalar(
        select(GeneratedAppBuildJob)
        .where(GeneratedAppBuildJob.app_id == app.id)
        .order_by(GeneratedAppBuildJob.created_at.desc())
        .limit(1)
    )
    return RuntimeBootstrap(app=app, revision=revision, deployment=deployment, build=build)


async def authorize_user(session: AsyncSession, *, app_id: UUID, user_id: UUID) -> AppActor:
    app = await _locked_active_app(session, app_id)
    return await _authorize_user_for_locked_app(
        session,
        app=app,
        user_id=user_id,
    )


async def _authorize_user_for_locked_app(
    session: AsyncSession,
    *,
    app: GeneratedApp,
    user_id: UUID,
) -> AppActor:
    """Re-read user authority while the caller holds the stable app-row lock."""

    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.id == app.conversation_id)
        .execution_options(populate_existing=True)
    )
    unclaimed_group = bool(
        conversation is not None
        and conversation.kind == ConversationKind.GROUP.value
        and conversation.group_owner_source == "unclaimed"
    )
    if app.user_id == user_id and not unclaimed_group:
        return AppActor(
            role=GeneratedAppRole.OWNER.value,
            user_id=user_id,
            identity_verified=True,
        )
    membership = await session.scalar(
        select(GeneratedAppMembership)
        .where(
            GeneratedAppMembership.app_id == app.id,
            GeneratedAppMembership.user_id == user_id,
        )
        .execution_options(populate_existing=True)
    )
    if membership is None or (unclaimed_group and membership.role == GeneratedAppRole.OWNER.value):
        raise CodeAppAuthorizationError("You do not have access to this app")
    return AppActor(role=membership.role, user_id=user_id, identity_verified=True)


async def authorize_session(session: AsyncSession, *, app_id: UUID, token: str) -> AppActor:
    try:
        app = await _locked_active_app(session, app_id)
    except CodeAppNotFoundError as error:
        raise CodeAppAuthorizationError("App session is invalid or expired") from error
    session_row = await session.scalar(
        select(GeneratedAppSession)
        .where(
            GeneratedAppSession.app_id == app_id,
            GeneratedAppSession.token_hash == _hash_token(token),
            GeneratedAppSession.revoked_at.is_(None),
            GeneratedAppSession.expires_at > datetime.now(UTC),
        )
        .execution_options(populate_existing=True)
    )
    if session_row is None:
        raise CodeAppAuthorizationError("App session is invalid or expired")
    return _session_actor(session_row, app=app)


async def issue_access_ticket(
    session: AsyncSession,
    *,
    app_id: UUID,
    issuer_user_id: UUID,
    principal_user_id: UUID | None,
    role: str,
    ttl_seconds: int = 900,
) -> str:
    app = await _locked_active_app(session, app_id)
    issuer = await _authorize_user_for_locked_app(
        session,
        app=app,
        user_id=issuer_user_id,
    )
    if issuer.role != GeneratedAppRole.OWNER.value:
        raise CodeAppAuthorizationError("Only the owner can issue app access")
    return await _persist_access_ticket(
        session,
        app_id=app_id,
        issuer_user_id=issuer_user_id,
        principal_user_id=principal_user_id,
        role=role,
        ttl_seconds=ttl_seconds,
    )


async def _issue_trusted_group_handoff_ticket(
    session: AsyncSession,
    *,
    app: GeneratedApp,
    conversation: Conversation,
    ttl_seconds: int,
) -> str:
    """Issue the builder's narrow group handoff without requiring a current human owner.

    A group can be temporarily unclaimed while a build is in flight. This path cannot mint owner
    authority: its bearer is always anonymous, group-scoped, and limited to the member role.
    """

    if (
        conversation.id != app.conversation_id
        or conversation.kind != ConversationKind.GROUP.value
        or app.access_mode != GeneratedAppAccessMode.COLLABORATIVE_LINK.value
    ):
        raise CodeAppAuthorizationError("Trusted group handoff requires a collaborative group app")
    return await _persist_access_ticket(
        session,
        app_id=app.id,
        # This is audit/FK metadata only. ``principal_user_id=None`` and the member role are the
        # actual authority, and a later owner claim rewrites the issuer to the successor.
        issuer_user_id=conversation.user_id,
        principal_user_id=None,
        role=GeneratedAppRole.MEMBER.value,
        ttl_seconds=ttl_seconds,
    )


async def _persist_access_ticket(
    session: AsyncSession,
    *,
    app_id: UUID,
    issuer_user_id: UUID,
    principal_user_id: UUID | None,
    role: str,
    ttl_seconds: int,
) -> str:
    if role not in {item.value for item in GeneratedAppRole}:
        raise CodeAppValidationError("Unsupported app role")
    raw = secrets.token_urlsafe(32)
    session.add(
        GeneratedAppAccessTicket(
            app_id=app_id,
            issued_by_user_id=issuer_user_id,
            principal_user_id=principal_user_id,
            role=role,
            token_hash=_hash_token(raw),
            expires_at=datetime.now(UTC)
            + timedelta(seconds=max(60, min(ttl_seconds, _MAX_ACCESS_TICKET_TTL_SECONDS))),
        )
    )
    await session.flush()
    return raw


async def redeem_access_ticket(
    session: AsyncSession, *, public_id: str, token: str, ttl_seconds: int = 30 * 86_400
) -> tuple[str, GeneratedAppSession]:
    app_statement = select(GeneratedApp).where(GeneratedApp.public_id == public_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        # Keep the same app -> ticket lock order used by archive, ownership transfer, and issue.
        app_statement = app_statement.with_for_update()
    app = await session.scalar(app_statement.execution_options(populate_existing=True))
    if app is None or app.status != GeneratedAppStatus.ACTIVE.value:
        raise CodeAppNotFoundError("App was not found")
    now = datetime.now(UTC)
    ticket_statement = select(GeneratedAppAccessTicket).where(
        GeneratedAppAccessTicket.app_id == app.id,
        GeneratedAppAccessTicket.token_hash == _hash_token(token),
        GeneratedAppAccessTicket.expires_at > now,
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        ticket_statement = ticket_statement.with_for_update()
    ticket = await session.scalar(ticket_statement.execution_options(populate_existing=True))
    if ticket is None:
        raise CodeAppAuthorizationError("Access ticket is invalid or expired")
    redemption_limit = (
        _MAX_COLLABORATIVE_TICKET_REDEMPTIONS
        if app.access_mode == GeneratedAppAccessMode.COLLABORATIVE_LINK.value
        else _MAX_PRIVATE_TICKET_REDEMPTIONS
    )
    if ticket.redemption_count >= redemption_limit:
        raise CodeAppAuthorizationError("Access ticket has reached its redemption limit")
    raw = secrets.token_urlsafe(32)
    app_session = GeneratedAppSession(
        app_id=app.id,
        user_id=ticket.principal_user_id,
        role=ticket.role,
        token_hash=_hash_token(raw),
        expires_at=now + timedelta(seconds=max(300, min(ttl_seconds, 30 * 86_400))),
    )
    ticket.used_at = ticket.used_at or now
    ticket.last_redeemed_at = now
    ticket.redemption_count += 1
    session.add(app_session)
    await session.commit()
    return raw, app_session


async def list_data_records(
    session: AsyncSession,
    *,
    app_id: UUID,
    actor: AppActor,
    entity: str,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[GeneratedAppDataRecord], int]:
    await _reauthorize_actor(session, app_id=app_id, actor=actor)
    clean_entity = _entity(entity)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    where = (
        GeneratedAppDataRecord.app_id == app_id,
        GeneratedAppDataRecord.entity == clean_entity,
    )
    total = await session.scalar(
        select(func.count()).select_from(GeneratedAppDataRecord).where(*where)
    )
    records = list(
        (
            await session.scalars(
                select(GeneratedAppDataRecord)
                .where(*where)
                .order_by(GeneratedAppDataRecord.created_at, GeneratedAppDataRecord.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    return records, int(total or 0)


async def create_data_record(
    session: AsyncSession,
    *,
    app_id: UUID,
    actor: AppActor,
    entity: str,
    data: dict[str, Any],
    idempotency_key: str,
) -> GeneratedAppDataRecord | StoredDataRecord:
    actor = await _reauthorize_actor(session, app_id=app_id, actor=actor)
    _require_write(actor)
    clean_entity = _entity(entity)
    if not isinstance(data, dict):
        raise CodeAppValidationError("Record data must be an object")
    clean_key = _text(idempotency_key, "idempotency_key", 160)
    request_hash = _idempotency_request_hash(
        "records.create",
        {"entity": clean_entity, "data": data},
    )
    existing = await _event_by_key(session, app_id, clean_key)
    if existing is not None:
        return _stored_record_from_event(
            existing,
            operation="records.create",
            request_hash=request_hash,
        )
    await _enforce_mutation_rate_limit(session, app_id=app_id, actor=actor)
    existing = await _event_by_key(session, app_id, clean_key)
    if existing is not None:
        return _stored_record_from_event(
            existing,
            operation="records.create",
            request_hash=request_hash,
        )
    # Read and validate the deployed schema only after taking the stable app-row lock. This
    # closes the race where a revision could deploy between validation and the record write.
    manifest = await _active_manifest(session, app_id)
    clean_data = _validate_entity_data(manifest, clean_entity, data)
    data_bytes = _json_bytes(clean_data)
    record_count = await session.scalar(
        select(func.count())
        .select_from(GeneratedAppDataRecord)
        .where(GeneratedAppDataRecord.app_id == app_id)
    )
    if int(record_count or 0) >= _MAX_RECORDS_PER_APP:
        raise CodeAppValidationError("This app has reached its record limit")
    await _enforce_app_storage_limit(session, app_id=app_id, added_bytes=data_bytes)
    record = GeneratedAppDataRecord(
        app_id=app_id,
        entity=clean_entity,
        data=clean_data,
        data_bytes=data_bytes,
        created_by_user_id=actor.user_id,
        updated_by_user_id=actor.user_id,
    )
    session.add(record)
    await session.flush()
    await _add_data_event(
        session,
        app_id=app_id,
        record=record,
        actor=actor,
        event_type="app.data.created",
        operation="records.create",
        request_hash=request_hash,
        idempotency_key=clean_key,
    )
    await session.commit()
    return record


async def update_data_record(
    session: AsyncSession,
    *,
    app_id: UUID,
    record_id: UUID,
    actor: AppActor,
    expected_version: int,
    data: dict[str, Any],
    idempotency_key: str,
) -> GeneratedAppDataRecord | StoredDataRecord:
    actor = await _reauthorize_actor(session, app_id=app_id, actor=actor)
    _require_write(actor)
    if not isinstance(data, dict):
        raise CodeAppValidationError("Record data must be an object")
    clean_key = _text(idempotency_key, "idempotency_key", 160)
    request_hash = _idempotency_request_hash(
        "records.update",
        {
            "record_id": str(record_id),
            "expected_version": expected_version,
            "data": data,
        },
    )
    existing = await _event_by_key(session, app_id, clean_key)
    if existing is not None:
        return _stored_record_from_event(
            existing,
            operation="records.update",
            request_hash=request_hash,
        )
    await _enforce_mutation_rate_limit(session, app_id=app_id, actor=actor)
    existing = await _event_by_key(session, app_id, clean_key)
    if existing is not None:
        return _stored_record_from_event(
            existing,
            operation="records.update",
            request_hash=request_hash,
        )
    manifest = await _active_manifest(session, app_id)
    record_state = (
        await session.execute(
            select(GeneratedAppDataRecord.entity).where(
                GeneratedAppDataRecord.id == record_id,
                GeneratedAppDataRecord.app_id == app_id,
            )
        )
    ).one_or_none()
    if record_state is None:
        raise CodeAppNotFoundError("App record was not found")
    clean_data = _validate_entity_data(manifest, record_state.entity, data)
    data_bytes = _json_bytes(clean_data)
    current_bytes = await session.scalar(
        select(GeneratedAppDataRecord.data_bytes).where(
            GeneratedAppDataRecord.id == record_id,
            GeneratedAppDataRecord.app_id == app_id,
        )
    )
    if current_bytes is None:
        raise CodeAppNotFoundError("App record was not found")
    await _enforce_app_storage_limit(
        session,
        app_id=app_id,
        added_bytes=max(0, data_bytes - current_bytes),
    )
    now = utc_now()
    changed_record_id = await session.scalar(
        update(GeneratedAppDataRecord)
        .where(
            GeneratedAppDataRecord.id == record_id,
            GeneratedAppDataRecord.app_id == app_id,
            GeneratedAppDataRecord.version == expected_version,
        )
        .values(
            data=clean_data,
            data_bytes=data_bytes,
            version=GeneratedAppDataRecord.version + 1,
            updated_by_user_id=actor.user_id,
            updated_at=now,
        )
        .returning(GeneratedAppDataRecord.id)
    )
    if changed_record_id is None:
        raise CodeAppConflictError("Record was changed by someone else")
    record = await session.scalar(
        select(GeneratedAppDataRecord)
        .where(GeneratedAppDataRecord.id == changed_record_id)
        .execution_options(populate_existing=True)
    )
    if record is None:  # pragma: no cover - protected by the successful CAS.
        raise CodeAppConflictError("Record disappeared while it was being updated")
    await _add_data_event(
        session,
        app_id=app_id,
        record=record,
        actor=actor,
        event_type="app.data.updated",
        operation="records.update",
        request_hash=request_hash,
        idempotency_key=clean_key,
    )
    await session.commit()
    return record


async def delete_data_record(
    session: AsyncSession,
    *,
    app_id: UUID,
    record_id: UUID,
    actor: AppActor,
    expected_version: int,
    idempotency_key: str,
) -> None:
    actor = await _reauthorize_actor(session, app_id=app_id, actor=actor)
    _require_write(actor)
    clean_key = _text(idempotency_key, "idempotency_key", 160)
    request_hash = _idempotency_request_hash(
        "records.delete",
        {"record_id": str(record_id), "expected_version": expected_version},
    )
    existing = await _event_by_key(session, app_id, clean_key)
    if existing is not None:
        _replay_response(
            existing,
            operation="records.delete",
            request_hash=request_hash,
        )
        return
    record_state = (
        await session.execute(
            select(
                GeneratedAppDataRecord.id,
                GeneratedAppDataRecord.entity,
                GeneratedAppDataRecord.data,
                GeneratedAppDataRecord.version,
            ).where(
                GeneratedAppDataRecord.id == record_id,
                GeneratedAppDataRecord.app_id == app_id,
            )
        )
    ).one_or_none()
    if record_state is None:
        raise CodeAppNotFoundError("App record was not found")
    await _enforce_mutation_rate_limit(session, app_id=app_id, actor=actor)
    existing = await _event_by_key(session, app_id, clean_key)
    if existing is not None:
        _replay_response(
            existing,
            operation="records.delete",
            request_hash=request_hash,
        )
        return
    deleted_record_id = await session.scalar(
        delete(GeneratedAppDataRecord)
        .where(
            GeneratedAppDataRecord.id == record_id,
            GeneratedAppDataRecord.app_id == app_id,
            GeneratedAppDataRecord.version == expected_version,
        )
        .returning(GeneratedAppDataRecord.id)
    )
    if deleted_record_id is None:
        raise CodeAppConflictError("Record was changed by someone else")
    session.add(
        GeneratedAppEvent(
            app_id=app_id,
            record_id=None,
            entity=record_state.entity,
            event_type="app.data.deleted",
            actor_user_id=actor.user_id,
            actor_session_id=actor.session_id,
            idempotency_key=clean_key,
            operation="records.delete",
            request_hash=request_hash,
            response={"deleted": True, "record_id": str(record_state.id)},
            payload={
                "tombstone": True,
                "record_id": str(record_state.id),
                "entity": record_state.entity,
                "version": record_state.version,
                "data": record_state.data,
            },
        )
    )
    await session.commit()


async def create_app_reminder(
    session: AsyncSession,
    *,
    app_id: UUID,
    actor: AppActor,
    title: str,
    goal: str,
    run_at: datetime,
    timezone: str,
    recurrence: str,
    idempotency_key: str,
) -> ScheduledTask | StoredScheduledTask:
    """Create the one deliberately narrow proactive capability available to generated apps."""

    actor = await _reauthorize_actor(session, app_id=app_id, actor=actor)
    if (
        not actor.identity_verified
        or actor.user_id is None
        or actor.role
        not in {
            GeneratedAppRole.OWNER.value,
            GeneratedAppRole.EDITOR.value,
        }
    ):
        raise CodeAppAuthorizationError(
            "Only an authenticated app owner or editor can create reminders"
        )
    app = await _code_app(session, app_id)
    clean_title = _text(title, "title", 160)
    clean_goal = _text(goal, "goal", 500)
    clean_timezone = _text(timezone, "timezone", 64)
    if not re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", clean_timezone):
        raise CodeAppValidationError("timezone must be an IANA timezone")
    if recurrence not in {item.value for item in ScheduledTaskRecurrence}:
        raise CodeAppValidationError("recurrence must be once, daily, or weekly")
    normalized_run_at = _aware_utc(run_at)
    now = datetime.now(UTC)
    if normalized_run_at > now + _MAX_REMINDER_HORIZON:
        raise CodeAppValidationError("Reminder time is too far in the future")
    clean_key = _text(idempotency_key, "idempotency_key", 160)
    request_hash = _idempotency_request_hash(
        "dot.reminder.create",
        {
            "title": clean_title,
            "goal": clean_goal,
            "run_at": normalized_run_at.isoformat(),
            "timezone": clean_timezone,
            "recurrence": recurrence,
        },
    )
    existing = await _event_by_key(session, app_id, clean_key)
    if existing is not None:
        return _stored_task_from_event(
            existing,
            operation="dot.reminder.create",
            request_hash=request_hash,
        )
    await _enforce_mutation_rate_limit(session, app_id=app_id, actor=actor)
    existing = await _event_by_key(session, app_id, clean_key)
    if existing is not None:
        return _stored_task_from_event(
            existing,
            operation="dot.reminder.create",
            request_hash=request_hash,
        )
    # Capability grants are revision-scoped. Read them while holding the app lock so a concurrent
    # revision cannot revoke reminder authority between this check and schedule creation.
    manifest = await _active_manifest(session, app_id)
    try:
        capabilities = parse_generated_app_capabilities(manifest)
    except GeneratedAppCapabilityError as error:
        raise CodeAppValidationError(str(error)) from error
    if DOT_REMINDER_CREATE_CAPABILITY not in capabilities:
        raise CodeAppAuthorizationError("This app was not granted reminder access")
    direct = await resolve_direct_conversation(session, user_id=actor.user_id)
    delivery_provider = await preferred_delivery_provider(session, conversation_id=direct.id)
    try:
        task = await create_scheduled_task(
            session,
            user_id=actor.user_id,
            conversation_id=direct.id,
            action_type=AGENT_REACHOUT_ACTION,
            source="generated_app",
            idempotency_key=f"generated_app.reminder:{app_id}:{clean_key}",
            title=clean_title,
            payload={
                "goal": clean_goal,
                "generated_app_id": str(app_id),
                "generated_app_title": app.title,
                # Generated apps may ask Dot to deliver this reminder, never to borrow the
                # user's integrations or capability tools when the schedule wakes.
                "tool_policy": "message_only",
            },
            run_at=normalized_run_at,
            timezone=clean_timezone,
            recurrence=recurrence,
            delivery_provider=delivery_provider,
        )
    except ScheduleValidationError as error:
        raise CodeAppValidationError(str(error)) from error
    session.add(
        GeneratedAppEvent(
            app_id=app_id,
            event_type="app.reminder.created",
            actor_user_id=actor.user_id,
            actor_session_id=actor.session_id,
            idempotency_key=clean_key,
            operation="dot.reminder.create",
            request_hash=request_hash,
            response={
                "schedule_id": str(task.id),
                "title": task.title,
                "goal": task.payload.get("goal"),
                "scheduled_for": task.scheduled_for.isoformat(),
                "timezone": task.timezone,
                "recurrence": task.recurrence,
                "delivery_provider": task.delivery_provider,
            },
            payload={
                "schedule_id": str(task.id),
                "title": task.title,
                "scheduled_for": task.scheduled_for.isoformat(),
                "timezone": task.timezone,
                "recurrence": task.recurrence,
            },
        )
    )
    await session.commit()
    return task


async def _active_blueprint(
    session: AsyncSession,
    *,
    app_id: UUID,
    revision_id: UUID,
) -> dict[str, Any]:
    job = await session.scalar(
        select(GeneratedAppBuildJob)
        .where(
            GeneratedAppBuildJob.app_id == app_id,
            GeneratedAppBuildJob.result_revision_id == revision_id,
        )
        .order_by(GeneratedAppBuildJob.created_at.desc())
        .limit(1)
    )
    if job is None:
        return {}
    request = job.request
    raw = request.get("blueprint", request) if isinstance(request, dict) else {}
    return dict(raw) if isinstance(raw, dict) else {}


async def _validate_existing_records_for_manifest(
    session: AsyncSession,
    *,
    app_id: UUID,
    manifest: dict[str, Any],
) -> None:
    records = (
        await session.scalars(
            select(GeneratedAppDataRecord).where(GeneratedAppDataRecord.app_id == app_id)
        )
    ).all()
    for record in records:
        try:
            _validate_entity_data(manifest, record.entity, record.data)
        except CodeAppValidationError as error:
            raise CodeAppValidationError(
                "The revised data schema is incompatible with existing app records"
            ) from error


def _revision_context(revision: GeneratedAppRevision) -> dict[str, Any]:
    artifact = revision.artifact if isinstance(revision.artifact, dict) else {}
    context: dict[str, Any] = {
        "revision_id": str(revision.id),
        "revision_number": revision.revision_number,
        "manifest": revision.manifest,
        "source_files": revision.source_files,
        "render_document": artifact.get("render_document"),
    }
    try:
        _require_json_size(context, "Base revision context", _MAX_REVISION_CONTEXT_BYTES)
    except CodeAppValidationError:
        context["source_files"] = {
            "omitted": True,
            "paths": sorted(revision.source_files)[:64],
        }
        try:
            _require_json_size(context, "Base revision context", _MAX_REVISION_CONTEXT_BYTES)
        except CodeAppValidationError:
            context["render_document"] = {"omitted": True}
            _require_json_size(context, "Base revision context", _MAX_REVISION_CONTEXT_BYTES)
    return context


async def _active_manifest(session: AsyncSession, app_id: UUID) -> dict[str, Any]:
    await _code_app(session, app_id)
    deployment = await session.get(GeneratedAppDeployment, app_id)
    if deployment is None:
        raise CodeAppConflictError("App is not deployed yet")
    revision = await session.get(GeneratedAppRevision, deployment.active_revision_id)
    if revision is None:
        raise CodeAppConflictError("Active app revision is missing")
    return revision.manifest


async def _code_app(session: AsyncSession, app_id: UUID) -> GeneratedApp:
    app = await session.scalar(
        select(GeneratedApp)
        .where(GeneratedApp.id == app_id)
        .execution_options(populate_existing=True)
    )
    if (
        app is None
        or app.status != GeneratedAppStatus.ACTIVE.value
        or app.runtime_kind != GeneratedAppRuntimeKind.CODE.value
    ):
        raise CodeAppNotFoundError("Code app was not found")
    return app


async def _locked_code_app(session: AsyncSession, app_id: UUID) -> GeneratedApp:
    app = await _locked_active_app(session, app_id)
    if app.runtime_kind != GeneratedAppRuntimeKind.CODE.value:
        raise CodeAppNotFoundError("Code app was not found")
    return app


async def _locked_active_app(session: AsyncSession, app_id: UUID) -> GeneratedApp:
    statement = select(GeneratedApp).where(GeneratedApp.id == app_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    app = await session.scalar(statement.execution_options(populate_existing=True))
    if app is None or app.status != GeneratedAppStatus.ACTIVE.value:
        raise CodeAppNotFoundError("App was not found")
    return app


async def _reauthorize_actor(
    session: AsyncSession,
    *,
    app_id: UUID,
    actor: AppActor,
) -> AppActor:
    """Lock the app, then reconstruct current authority from durable identity state.

    ``AppActor`` is an authorization snapshot. It must never remain authoritative across an
    ownership transfer, archive, session revocation, or role change.
    """

    app = await _locked_code_app(session, app_id)
    if actor.session_id is not None:
        row = await session.scalar(
            select(GeneratedAppSession)
            .where(
                GeneratedAppSession.id == actor.session_id,
                GeneratedAppSession.app_id == app.id,
                GeneratedAppSession.revoked_at.is_(None),
                GeneratedAppSession.expires_at > datetime.now(UTC),
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise CodeAppAuthorizationError("App session is invalid or expired")
        return _session_actor(row, app=app)
    if actor.user_id is None:
        raise CodeAppAuthorizationError("App actor identity is missing")
    return await _authorize_user_for_locked_app(
        session,
        app=app,
        user_id=actor.user_id,
    )


def _session_actor(row: GeneratedAppSession, *, app: GeneratedApp) -> AppActor:
    if row.app_id != app.id:
        raise CodeAppAuthorizationError("App session is invalid or expired")
    return AppActor(
        role=row.role,
        user_id=row.user_id,
        session_id=row.id,
        # A handoff session is bearer access to this app, not fresh proof that the
        # browser controls the owner's Dot account.
        identity_verified=False,
    )


async def _locked_owned_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    user_id: UUID,
    requester_user_id: UUID,
) -> Conversation:
    statement = select(Conversation).where(Conversation.id == conversation_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        # Ownership transfer takes this lock before moving the group's apps. Serializing creation
        # here prevents a newly committed app from being left with a departed or unclaimed owner.
        statement = statement.with_for_update()
    conversation = await session.scalar(statement.execution_options(populate_existing=True))
    if conversation is None or conversation.user_id != user_id:
        raise CodeAppValidationError("Conversation does not belong to this user")
    if conversation.kind != ConversationKind.GROUP.value:
        if requester_user_id != user_id:
            raise CodeAppAuthorizationError("Conversation does not belong to this user")
        return conversation
    if conversation.group_owner_source == "unclaimed":
        raise CodeAppAuthorizationError("This group does not currently have an app owner")
    membership_statement = select(ConversationMember).where(
        ConversationMember.conversation_id == conversation.id,
        ConversationMember.user_id.in_({user_id, requester_user_id}),
        ConversationMember.status == ConversationMemberStatus.ACTIVE.value,
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        membership_statement = membership_statement.with_for_update()
    memberships = list(
        (
            await session.scalars(
                membership_statement.execution_options(populate_existing=True)
            )
        ).all()
    )
    owner_membership = next(
        (
            membership
            for membership in memberships
            if membership.user_id == user_id
            and membership.role == ConversationMemberRole.OWNER.value
        ),
        None,
    )
    if owner_membership is None:
        raise CodeAppAuthorizationError("Only the active group owner can create an app")
    if not any(membership.user_id == requester_user_id for membership in memberships):
        raise CodeAppAuthorizationError("The requester is not an active group member")
    return conversation


async def _ensure_no_live_build(session: AsyncSession, *, app_id: UUID) -> None:
    live = await session.scalar(
        select(GeneratedAppBuildJob.id)
        .where(
            GeneratedAppBuildJob.app_id == app_id,
            GeneratedAppBuildJob.status.in_(
                {
                    GeneratedAppBuildStatus.QUEUED.value,
                    GeneratedAppBuildStatus.CLAIMED.value,
                }
            ),
        )
        .limit(1)
    )
    if live is not None:
        raise CodeAppConflictError(
            "This app already has a revision in progress; wait for it to finish"
        )


async def _claimed_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_attempt: int,
) -> GeneratedAppBuildJob:
    if (
        not isinstance(expected_attempt, int)
        or isinstance(expected_attempt, bool)
        or expected_attempt < 1
    ):
        raise CodeAppConflictError("Build claim attempt is invalid")
    # The attempt is a fencing token. Locking the matching row makes settlement atomic with a
    # concurrent lease reclaim: once another worker (or a restarted worker with the same ID)
    # increments the attempt, the stale build can no longer publish or fail the job.
    statement = select(GeneratedAppBuildJob).where(
        GeneratedAppBuildJob.id == job_id,
        GeneratedAppBuildJob.status == GeneratedAppBuildStatus.CLAIMED.value,
        GeneratedAppBuildJob.claimed_by == worker_id,
        GeneratedAppBuildJob.attempts == expected_attempt,
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    job = await session.scalar(statement.execution_options(populate_existing=True))
    if job is None:
        raise CodeAppConflictError("Build claim is stale or belongs to another worker")
    return job


async def _event_by_key(
    session: AsyncSession, app_id: UUID, idempotency_key: str
) -> GeneratedAppEvent | None:
    return await session.scalar(
        select(GeneratedAppEvent).where(
            GeneratedAppEvent.app_id == app_id,
            GeneratedAppEvent.idempotency_key == _text(idempotency_key, "idempotency_key", 160),
        )
    )


async def _build_by_idempotency_key(
    session: AsyncSession,
    idempotency_key: str,
) -> GeneratedAppBuildJob | None:
    return await session.scalar(
        select(GeneratedAppBuildJob).where(
            GeneratedAppBuildJob.idempotency_key == idempotency_key
        )
    )


def _validate_build_replay(
    job: GeneratedAppBuildJob,
    *,
    request_hash: str,
) -> None:
    if job.request_hash != request_hash:
        raise CodeAppConflictError(
            "This idempotency key was already used for a different app build"
        )


async def _replay_created_build(
    session: AsyncSession,
    *,
    job: GeneratedAppBuildJob,
    request_hash: str,
) -> tuple[GeneratedApp, GeneratedAppBuildJob, str]:
    _validate_build_replay(job, request_hash=request_hash)
    app = await session.get(GeneratedApp, job.app_id)
    if app is None:
        raise CodeAppConflictError("Stored app-build response is unavailable")
    _, marker, ticket = job.app_url.partition("#handoff=")
    return app, job, ticket if marker else ""


def _replay_response(
    event: GeneratedAppEvent,
    *,
    operation: str,
    request_hash: str,
) -> dict[str, Any]:
    if event.operation != operation or event.request_hash != request_hash:
        raise CodeAppConflictError(
            "This idempotency key was already used for a different app mutation"
        )
    if not isinstance(event.response, dict):
        raise CodeAppConflictError("Stored idempotency response is unavailable")
    return dict(event.response)


def _stored_record_from_event(
    event: GeneratedAppEvent,
    *,
    operation: str,
    request_hash: str,
) -> StoredDataRecord:
    response = _replay_response(
        event,
        operation=operation,
        request_hash=request_hash,
    )
    try:
        return StoredDataRecord(
            id=UUID(str(response["record_id"])),
            entity=str(response["entity"]),
            data=dict(response["data"]),
            version=int(response["version"]),
            created_at=datetime.fromisoformat(str(response["created_at"])),
            updated_at=datetime.fromisoformat(str(response["updated_at"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CodeAppConflictError("Stored idempotency response is unavailable") from error


def _stored_rollback_from_event(
    event: GeneratedAppEvent,
    *,
    app: GeneratedApp,
    request_hash: str,
) -> StoredRollback:
    response = _replay_response(
        event,
        operation="deployment.rollback",
        request_hash=request_hash,
    )
    try:
        if UUID(str(response["app_id"])) != app.id:
            raise ValueError("stored app does not match")
        return StoredRollback(
            app_id=app.id,
            public_id=str(response["public_id"]),
            title=str(response["title"]),
            active_revision_id=UUID(str(response["active_revision_id"])),
            active_revision_number=int(response["active_revision_number"]),
            deployment_version=int(response["deployment_version"]),
            rollback_is_reversible=bool(response["rollback_is_reversible"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CodeAppConflictError("Stored rollback response is unavailable") from error


def _stored_task_from_event(
    event: GeneratedAppEvent,
    *,
    operation: str,
    request_hash: str,
) -> StoredScheduledTask:
    response = _replay_response(
        event,
        operation=operation,
        request_hash=request_hash,
    )
    try:
        return StoredScheduledTask(
            id=UUID(str(response["schedule_id"])),
            title=str(response["title"]),
            payload={"goal": response.get("goal")},
            scheduled_for=datetime.fromisoformat(str(response["scheduled_for"])),
            timezone=str(response["timezone"]),
            recurrence=str(response["recurrence"]),
            delivery_provider=(
                str(response["delivery_provider"])
                if response.get("delivery_provider") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CodeAppConflictError("Stored idempotency response is unavailable") from error


async def _enforce_mutation_rate_limit(
    session: AsyncSession,
    *,
    app_id: UUID,
    actor: AppActor,
) -> None:
    """Serialize and count durable app mutations for one authenticated user or app session."""

    if actor.user_id is None and actor.session_id is None:
        raise CodeAppAuthorizationError("App actor identity is missing")
    lock = select(GeneratedApp.id).where(
        GeneratedApp.id == app_id,
        GeneratedApp.status == GeneratedAppStatus.ACTIVE.value,
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        lock = lock.with_for_update()
    if await session.scalar(lock) is None:
        raise CodeAppNotFoundError("App was not found")
    actor_filter = (
        GeneratedAppEvent.actor_user_id == actor.user_id
        if actor.user_id is not None
        else GeneratedAppEvent.actor_session_id == actor.session_id
    )
    mutation_count = await session.scalar(
        select(func.count())
        .select_from(GeneratedAppEvent)
        .where(
            GeneratedAppEvent.app_id == app_id,
            actor_filter,
            GeneratedAppEvent.created_at >= datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    if int(mutation_count or 0) >= _MAX_MUTATIONS_PER_ACTOR_MINUTE:
        raise CodeAppRateLimitError("This app is doing too much at once. Try again shortly.")
    app_mutation_count = await session.scalar(
        select(func.count())
        .select_from(GeneratedAppEvent)
        .where(
            GeneratedAppEvent.app_id == app_id,
            GeneratedAppEvent.created_at >= datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    if int(app_mutation_count or 0) >= _MAX_MUTATIONS_PER_APP_MINUTE:
        raise CodeAppRateLimitError("This app is doing too much at once. Try again shortly.")


async def _enforce_user_app_build_quota(
    session: AsyncSession,
    *,
    user_id: UUID,
    creating_app: bool,
) -> None:
    """Bound user-owned app inventory and concurrent paid build work before enqueueing."""

    user_statement = select(User.id).where(User.id == user_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        user_statement = user_statement.with_for_update()
    if await session.scalar(user_statement) is None:
        raise CodeAppNotFoundError("User was not found")
    if creating_app:
        active_apps = await session.scalar(
            select(func.count())
            .select_from(GeneratedApp)
            .where(
                GeneratedApp.user_id == user_id,
                GeneratedApp.status == GeneratedAppStatus.ACTIVE.value,
                GeneratedApp.runtime_kind == GeneratedAppRuntimeKind.CODE.value,
            )
        )
        if int(active_apps or 0) >= _MAX_ACTIVE_CODE_APPS_PER_USER:
            raise CodeAppValidationError(
                "You have reached the active custom-app limit. Archive one before creating another."
            )
    live_builds = await session.scalar(
        select(func.count())
        .select_from(GeneratedAppBuildJob)
        .join(GeneratedApp, GeneratedApp.id == GeneratedAppBuildJob.app_id)
        .where(
            GeneratedApp.user_id == user_id,
            GeneratedAppBuildJob.status.in_(
                {
                    GeneratedAppBuildStatus.QUEUED.value,
                    GeneratedAppBuildStatus.CLAIMED.value,
                }
            ),
        )
    )
    if int(live_builds or 0) >= _MAX_LIVE_BUILDS_PER_USER:
        raise CodeAppRateLimitError(
            "You already have several custom apps building. Wait for one to finish and try again."
        )


async def _enforce_app_storage_limit(
    session: AsyncSession,
    *,
    app_id: UUID,
    added_bytes: int,
) -> None:
    if added_bytes <= 0:
        return
    used_bytes = await session.scalar(
        select(func.coalesce(func.sum(GeneratedAppDataRecord.data_bytes), 0)).where(
            GeneratedAppDataRecord.app_id == app_id
        )
    )
    if int(used_bytes or 0) + added_bytes > _MAX_DATA_BYTES_PER_APP:
        raise CodeAppValidationError("This app has reached its storage limit")


async def _add_data_event(
    session: AsyncSession,
    *,
    app_id: UUID,
    record: GeneratedAppDataRecord,
    actor: AppActor,
    event_type: str,
    operation: str,
    request_hash: str,
    idempotency_key: str,
) -> None:
    response = {
        "record_id": str(record.id),
        "entity": record.entity,
        "data": record.data,
        "version": record.version,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }
    session.add(
        GeneratedAppEvent(
            app_id=app_id,
            record_id=record.id,
            entity=record.entity,
            event_type=event_type,
            actor_user_id=actor.user_id,
            actor_session_id=actor.session_id,
            idempotency_key=_text(idempotency_key, "idempotency_key", 160),
            operation=operation,
            request_hash=request_hash,
            response=response,
            payload={"record_id": str(record.id), "version": record.version, "data": record.data},
        )
    )


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict):
        raise CodeAppValidationError("Manifest must be an object")
    if manifest.get("schema_version") != 1:
        raise CodeAppValidationError("Unsupported manifest schema_version")
    try:
        parse_generated_app_capabilities(manifest)
    except GeneratedAppCapabilityError as error:
        raise CodeAppValidationError(str(error)) from error
    _require_json_size(manifest, "Manifest", _MAX_MANIFEST_BYTES)
    entities = manifest.get("entities", [])
    if not isinstance(entities, list) or len(entities) > 24:
        raise CodeAppValidationError("Manifest requires at most 24 entities")
    seen: set[str] = set()
    for value in entities:
        if not isinstance(value, dict):
            raise CodeAppValidationError("Entity definitions must be objects")
        name = _entity(value.get("name"))
        if name in seen:
            raise CodeAppValidationError("Entity names must be unique")
        seen.add(name)
        fields = _entity_fields(value)
        if len(fields) > 64:
            raise CodeAppValidationError("An entity can have at most 64 fields")
        for definition in fields.values():
            if definition.get("type") not in {
                "string",
                "text",
                "number",
                "integer",
                "money",
                "currency",
                "boolean",
                "date",
                "datetime",
                "email",
                "url",
                "enum",
                "relation",
                "object",
                "array",
            }:
                raise CodeAppValidationError("Unsupported entity field type")


def _validate_entity_data(
    manifest: dict[str, Any], entity: str, data: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise CodeAppValidationError("Record data must be an object")
    definition = next(
        (item for item in manifest.get("entities", []) if item.get("name") == entity), None
    )
    if definition is None:
        raise CodeAppValidationError("Entity is not declared by the active revision")
    fields = _entity_fields(definition)
    unknown = set(data) - set(fields)
    if unknown:
        raise CodeAppValidationError(f"Unknown fields: {', '.join(sorted(unknown))}")
    missing = {
        name
        for name, field in fields.items()
        if field.get("required") is True and (name not in data or data[name] is None)
    }
    if missing:
        raise CodeAppValidationError(f"Missing required fields: {', '.join(sorted(missing))}")
    clean: dict[str, Any] = {}
    for name, value in data.items():
        _validate_field_value(name, fields[name]["type"], value)
        clean[name] = value
    _require_json_size(clean, "Record data", _MAX_RECORD_BYTES)
    return clean


def _validate_field_value(name: str, kind: str, value: Any) -> None:
    if value is None:
        return
    valid = {
        "string": isinstance(value, str),
        "text": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "money": isinstance(value, (int, float)) and not isinstance(value, bool),
        "currency": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "email": isinstance(value, str),
        "url": isinstance(value, str),
        "enum": isinstance(value, str),
        "relation": isinstance(value, str),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "date": isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)),
        "datetime": isinstance(value, str),
    }[kind]
    if not valid:
        raise CodeAppValidationError(f"Field {name} must be {kind}")


def _require_write(actor: AppActor) -> None:
    if actor.role not in _WRITABLE_ROLES:
        raise CodeAppAuthorizationError("This app session is read-only")


def _entity_fields(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_fields = definition.get("fields", {})
    if isinstance(raw_fields, dict):
        result: dict[str, dict[str, Any]] = {}
        for name, value in raw_fields.items():
            clean_name = _entity(name)
            if not isinstance(value, dict):
                raise CodeAppValidationError("Entity field definitions must be objects")
            result[clean_name] = value
        return result
    if isinstance(raw_fields, list):
        result = {}
        for value in raw_fields:
            if not isinstance(value, dict):
                raise CodeAppValidationError("Entity field definitions must be objects")
            name = _entity(value.get("name"))
            if name in result:
                raise CodeAppValidationError("Entity field names must be unique")
            result[name] = value
        return result
    raise CodeAppValidationError("Entity fields must be an object or list")


def _entity(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise CodeAppValidationError("Entity and field names must be snake_case identifiers")
    return value


def _generated_app_access_mode(value: Any) -> str:
    supported = {mode.value for mode in GeneratedAppAccessMode}
    if not isinstance(value, str) or value not in supported:
        raise CodeAppValidationError("Unsupported generated-app access mode")
    return value


def _text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodeAppValidationError(f"{field} is required")
    clean = value.strip()
    if len(clean) > max_length:
        raise CodeAppValidationError(f"{field} is too long")
    return clean


def _optional_text(value: Any, max_length: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CodeAppValidationError("Text value is invalid")
    return value.strip()[:max_length]


def _fallback_text(value: Any, fallback: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return value.strip()[:max_length]


def _json_object(value: Any, *, default: dict[str, Any]) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else dict(default)


def _idempotency_request_hash(operation: str, request: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            {"operation": operation, "request": request},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as error:
        raise CodeAppValidationError("Mutation request must contain only JSON values") from error
    return hashlib.sha256(encoded).hexdigest()


def _validated_request_hash(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CodeAppValidationError("Idempotency request hash is invalid")
    return value


def _require_json_size(value: Any, field: str, max_bytes: int) -> None:
    encoded_bytes = _json_bytes(value, field=field)
    if encoded_bytes > max_bytes:
        raise CodeAppValidationError(f"{field} is too large")


def _json_bytes(value: Any, *, field: str = "JSON value") -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as error:
        raise CodeAppValidationError(f"{field} must contain only JSON values") from error
    return len(encoded)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CodeAppValidationError("run_at must include a timezone offset")
    return value.astimezone(UTC)


def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _build_purpose(request: dict[str, Any], fallback: str) -> str:
    blueprint = request.get("blueprint", request)
    if not isinstance(blueprint, dict):
        return fallback
    purpose = blueprint.get("purpose")
    return purpose[:500] if isinstance(purpose, str) else fallback


def _safe_failure(raw_error: str) -> tuple[str, bool | None]:
    try:
        parsed = json.loads(raw_error)
    except (TypeError, ValueError):
        return "build_failed", None
    if not isinstance(parsed, dict):
        return "build_failed", None
    raw_code = parsed.get("code")
    code = raw_code if isinstance(raw_code, str) and len(raw_code) <= 64 else "build_failed"
    retryable = parsed.get("retryable")
    return code, retryable if isinstance(retryable, bool) else None


def _safe_build_telemetry(
    artifact: dict[str, Any] | None,
    test_results: dict[str, Any] | None,
    build_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    provider = (artifact or {}).get("provider_metadata", {})
    if not isinstance(provider, dict):
        provider = {}
    explicit_provider = (build_metadata or {}).get("provider_metadata", {})
    if isinstance(explicit_provider, dict):
        provider = {**provider, **explicit_provider}
    telemetry: dict[str, Any] = {}
    model = provider.get("model")
    if isinstance(model, str) and len(model) <= 120:
        telemetry["model"] = model
    token_usage = provider.get("token_usage")
    if isinstance(token_usage, dict):
        safe_tokens = {
            key: value
            for key, value in token_usage.items()
            if key in {"input_tokens", "output_tokens", "total_tokens"}
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        }
        if safe_tokens:
            telemetry["token_usage"] = safe_tokens
    cost = provider.get("cost_usd")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool):
        build_metrics = (test_results or {}).get("build_metrics", {})
        cost = build_metrics.get("cost_usd") if isinstance(build_metrics, dict) else None
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        telemetry["cost_usd"] = float(cost)
    return telemetry
