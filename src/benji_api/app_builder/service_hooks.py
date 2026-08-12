from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from benji_api.app_builder.types import (
    BuildClaim,
    BuildCompletion,
    BuildCompletionHookError,
    BuildFailure,
    canonical_json,
)
from benji_api.config import Settings, get_settings
from benji_api.db.session import async_session_factory
from benji_api.models.generated_app import GeneratedApp


class GeneratedAppBuildServiceHooks:
    """Thin adapter between the provider-neutral builder and durable app services."""

    def __init__(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.session_factory = session_factory or async_session_factory
        self.settings = settings or get_settings()

    async def claim_next_build(self) -> BuildClaim | None:
        from benji_api.services.generated_apps_v2 import claim_next_build

        async with self.session_factory() as session:
            job = await claim_next_build(
                session,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
        if job is None:
            return None
        request = _mapping(job.request, field_name="build request")
        blueprint = request.get("blueprint", request)
        if not isinstance(blueprint, dict):
            blueprint = {"invalid_blueprint": blueprint}
        return BuildClaim(
            job_id=str(job.job_id),
            app_id=str(job.app_id),
            revision_id=str(job.base_revision_id) if job.base_revision_id else None,
            blueprint=blueprint,
            attempt=int(job.attempt),
        )

    async def complete_build(self, claim: BuildClaim, completion: BuildCompletion) -> None:
        from benji_api.services.generated_apps_v2 import complete_build

        artifact = completion.artifact
        artifact_payload = artifact.as_dict()
        async with self.session_factory() as session:
            try:
                app_url = await self._app_base_url(session, claim)
                await complete_build(
                    session,
                    job_id=UUID(claim.job_id),
                    worker_id=self.worker_id,
                    expected_attempt=claim.attempt,
                    manifest=dict(artifact.manifest),
                    source_files={item.path: item.contents for item in artifact.files},
                    artifact=artifact_payload,
                    artifact_url=f"artifact://sha256/{artifact.content_hash}",
                    artifact_sha256=artifact.content_hash,
                    sdk_version=artifact.sdk_version,
                    dependency_lock=dict(artifact.dependency_lock),
                    test_results={
                        **dict(artifact.test_results),
                        "build_metrics": completion.metrics.as_dict(),
                    },
                    handoff_base_url=app_url,
                    build_metadata={
                        "metrics": completion.metrics.as_dict(),
                        "provider": artifact.provider,
                        "provider_version": artifact.provider_version,
                        "provider_metadata": dict(artifact.provider_metadata),
                    },
                )
            except Exception as error:
                code, retryable = _classify_completion_error(error)
                try:
                    await session.rollback()
                except Exception as rollback_error:
                    raise BuildCompletionHookError(
                        code="completion_rollback_error",
                        message=(
                            f"{type(error).__name__}: {error}; rollback failed with "
                            f"{type(rollback_error).__name__}: {rollback_error}"
                        ),
                        retryable=True,
                    ) from rollback_error
                raise BuildCompletionHookError(
                    code=code,
                    message=f"{type(error).__name__}: {error}",
                    retryable=retryable,
                ) from error

    async def fail_build(self, claim: BuildClaim, failure: BuildFailure) -> None:
        from benji_api.services.generated_apps_v2 import fail_build

        error = canonical_json(
            {
                "code": failure.code,
                "message": failure.message,
                "retryable": failure.retryable,
                "duration_ms": failure.duration_ms,
                "issues": [issue.as_dict() for issue in failure.issues],
            }
        )
        async with self.session_factory() as session:
            app = await session.get(GeneratedApp, UUID(claim.app_id))
            app_url = (
                f"{self.settings.generated_app_public_url}/a/{app.public_id}"
                if app is not None
                else None
            )
            await fail_build(
                session,
                job_id=UUID(claim.job_id),
                worker_id=self.worker_id,
                expected_attempt=claim.attempt,
                error=error[:4_000],
                app_url=app_url,
                retryable=failure.retryable,
                build_metadata={
                    "duration_ms": failure.duration_ms,
                    "issues": [issue.as_dict() for issue in failure.issues],
                },
            )

    async def _app_base_url(self, session: AsyncSession, claim: BuildClaim) -> str:
        from benji_api.services.generated_apps_v2 import CodeAppNotFoundError

        app = await session.get(GeneratedApp, UUID(claim.app_id))
        if app is None:
            raise CodeAppNotFoundError("Claimed app no longer exists")
        return f"{self.settings.generated_app_public_url}/a/{app.public_id}"


def _mapping(value: object, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be an object")
    return value


def _classify_completion_error(error: Exception) -> tuple[str, bool]:
    from benji_api.services.generated_apps_v2 import (
        CodeAppConflictError,
        CodeAppNotFoundError,
        CodeAppStaleBuildError,
        CodeAppValidationError,
    )

    if isinstance(error, CodeAppStaleBuildError):
        return "stale_base_revision", False
    if isinstance(error, CodeAppValidationError):
        return "completion_validation_error", False
    if isinstance(error, CodeAppNotFoundError):
        return "completion_target_missing", False
    if isinstance(error, (CodeAppConflictError, IntegrityError)):
        return "completion_conflict", False
    if isinstance(error, (TypeError, ValueError)):
        return "completion_validation_error", False
    # Connection failures, timeouts, and unexpected infrastructure faults get the queue's
    # existing bounded retry budget. After the final claim, fail_build marks them terminal.
    return "completion_persistence_error", True
