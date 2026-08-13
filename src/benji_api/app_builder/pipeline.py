from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import Any

from benji_api.app_builder.browser_smoke import AppBrowserSmokeError
from benji_api.app_builder.compiler import AppCompilationError, EsbuildAppCompiler
from benji_api.app_builder.policy import inspect_generated_source
from benji_api.app_builder.types import (
    MAX_STATIC_HTML_BYTES,
    AppBlueprint,
    AppBrowserSmokeRunner,
    AppCompiler,
    AppSourceProvider,
    BlueprintValidationError,
    BrowserBundle,
    BuildArtifact,
    BuildClaim,
    BuildCompletion,
    BuildCompletionHookError,
    BuildFailure,
    BuildJobHooks,
    BuildMetrics,
    BuildStage,
    GeneratedSource,
    ValidationIssue,
    canonical_json,
    content_hash,
)

DOT_APP_SDK_VERSION = "2"
logger = logging.getLogger(__name__)
_MAX_BUILD_ARTIFACT_BYTES = 4_000_000
_MAX_TEST_RESULTS_BYTES = 128_000
_COMPUTED_ACCEPTANCE_FIELDS = frozenset({"created_at", "updated_at", "id", "version"})
_COMPILATION_BLOCKING_POLICY_CODES = frozenset(
    {
        "duplicate_source_path",
        "file_too_large",
        "invalid_source_path",
        "invalid_typescript",
        "missing_entrypoint",
        "missing_source",
        "source_too_large",
        "too_many_files",
    }
)
_ACCEPTANCE_ISSUE_PREFIX = "acceptance_"
DOT_APP_DEPENDENCY_LOCK = MappingProxyType(
    {
        "@dot/app-runtime": "2",
        "@dot/ui": "2",
        "date-fns": "4.1.0",
        "lucide-react": "0.468.0",
        "motion": "12.0.0",
        "react": "19.2.0",
        "react-dom": "19.2.0",
        "recharts": "2.15.0",
    }
)


class BuildRejectedError(RuntimeError):
    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        super().__init__("generated source did not pass build policy")


@dataclass(slots=True)
class _StageTimer:
    durations: dict[str, int]

    async def measure[T](
        self,
        stage: BuildStage,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        started = monotonic()
        try:
            return await operation()
        finally:
            elapsed = max(0, round((monotonic() - started) * 1000))
            self.durations[stage] = self.durations.get(stage, 0) + elapsed


class AppBuildPipeline:
    def __init__(
        self,
        provider: AppSourceProvider,
        *,
        max_repair_attempts: int = 2,
        timeout_seconds: float = 55.0,
        target_duration_ms: int = 60_000,
        compiler: AppCompiler | None = None,
        smoke_runner: AppBrowserSmokeRunner | None = None,
        require_browser_smoke: bool = False,
        visual_reviewer: Any | None = None,
        max_visual_reviews: int = 2,
    ) -> None:
        if max_repair_attempts < 0 or max_repair_attempts > 4:
            raise ValueError("max_repair_attempts must be between 0 and 4")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if require_browser_smoke and smoke_runner is None:
            raise ValueError("require_browser_smoke needs a configured smoke runner")
        if max_visual_reviews < 1 or max_visual_reviews > 4:
            raise ValueError("max_visual_reviews must be between 1 and 4")
        self.provider = provider
        self.max_repair_attempts = max_repair_attempts
        self.timeout_seconds = timeout_seconds
        self.target_duration_ms = target_duration_ms
        self.compiler = compiler or EsbuildAppCompiler(timeout_seconds=min(20.0, timeout_seconds))
        self.smoke_runner = smoke_runner
        self.require_browser_smoke = require_browser_smoke
        self.visual_reviewer = visual_reviewer
        self.max_visual_reviews = max_visual_reviews

    async def build(self, claim: BuildClaim) -> BuildCompletion:
        started_at = datetime.now(UTC)
        started = monotonic()
        stages = _StageTimer({})
        attempts = 0
        repairs = 0
        issue_history: list[tuple[ValidationIssue, ...]] = []
        seen_acceptance_fingerprints: set[tuple[tuple[str, str, str], ...]] = set()
        clean_regeneration_used = False
        visual_reviews = 0
        screenshot_dir: str | None = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                blueprint = await stages.measure(
                    "validate", lambda: _as_awaitable(AppBlueprint.from_mapping(claim.blueprint))
                )
                attempts = 1
                generated = await stages.measure(
                    "generate", lambda: self.provider.generate(blueprint)
                )
                browser_bundle = None
                smoke_result: MappingProxyType[str, object] | None = None
                while True:
                    browser_bundle = None
                    smoke_result = None
                    policy_issues = await stages.measure(
                        "inspect",
                        lambda current=generated: _as_awaitable(
                            inspect_generated_source(current)
                        ),
                    )
                    issues = policy_issues
                    if not any(
                        issue.code in _COMPILATION_BLOCKING_POLICY_CODES
                        for issue in policy_issues
                    ):
                        try:
                            browser_bundle = await stages.measure(
                                "compile",
                                lambda current=generated: self.compiler.compile(current),
                            )
                        except AppCompilationError as exc:
                            issues = _dedupe_issues((*policy_issues, *exc.issues))
                    if not issues:
                        if self.smoke_runner is not None and browser_bundle is not None:
                            screenshot_file: Path | None = None
                            if (
                                self.visual_reviewer is not None
                                and visual_reviews < self.max_visual_reviews
                            ):
                                if screenshot_dir is None:
                                    screenshot_dir = tempfile.mkdtemp(prefix="dot-app-visual-")
                                screenshot_file = Path(screenshot_dir) / "at-rest.jpeg"
                                screenshot_file.unlink(missing_ok=True)
                            smoke_kwargs: dict[str, Any] = {
                                "acceptance_plan": _acceptance_plan(blueprint),
                            }
                            if screenshot_file is not None:
                                smoke_kwargs["screenshot_path"] = screenshot_file
                            try:
                                result = await stages.measure(
                                    "smoke",
                                    partial(
                                        self.smoke_runner.smoke,
                                        browser_bundle,
                                        **smoke_kwargs,
                                    ),
                                )
                                smoke_result = MappingProxyType(dict(result))
                            except AppBrowserSmokeError as exc:
                                issues = exc.issues
                            if (
                                not issues
                                and screenshot_file is not None
                                and screenshot_file.is_file()
                            ):
                                screenshot_jpeg = screenshot_file.read_bytes()
                                if screenshot_jpeg:
                                    visual_reviews += 1
                                    issues = await self._review_visual_quality(
                                        stages,
                                        screenshot_jpeg,
                                        blueprint,
                                        claim,
                                    )
                        else:
                            if self.require_browser_smoke:
                                raise RuntimeError("required browser smoke runner is unavailable")
                            smoke_result = MappingProxyType(
                                {"ready": None, "status": "not_configured"}
                            )
                    if not issues:
                        break
                    issue_history.append(issues)
                    acceptance_fingerprint = _acceptance_issue_fingerprint(issues)
                    repeated_acceptance = (
                        acceptance_fingerprint is not None
                        and acceptance_fingerprint in seen_acceptance_fingerprints
                    )
                    if acceptance_fingerprint is not None:
                        seen_acceptance_fingerprints.add(acceptance_fingerprint)
                    logger.info(
                        "Generated app candidate needs repair job_id=%s attempt=%s issues=%s",
                        claim.job_id,
                        attempts,
                        _safe_issue_log_payload(issues),
                    )
                    if repeated_acceptance and clean_regeneration_used:
                        logger.info(
                            "Generated app convergence stopped job_id=%s attempt=%s "
                            "reason=repeated_acceptance_after_clean_regeneration issues=%s",
                            claim.job_id,
                            attempts,
                            _safe_issue_log_payload(issues),
                        )
                        raise BuildRejectedError(issues)
                    if repairs >= self.max_repair_attempts:
                        raise BuildRejectedError(issues)
                    repairs += 1
                    attempts += 1
                    if repeated_acceptance:
                        clean_regeneration_used = True
                        diagnostics = _accumulated_diagnostics(issue_history)
                        logger.info(
                            "Generated app convergence reset job_id=%s attempt=%s "
                            "reason=repeated_acceptance issues=%s",
                            claim.job_id,
                            attempts,
                            _safe_issue_log_payload(diagnostics),
                        )
                        generated = await stages.measure(
                            "repair",
                            partial(
                                _regenerate_with_diagnostics,
                                self.provider,
                                blueprint,
                                diagnostics,
                                attempt=repairs,
                            ),
                        )
                    else:
                        previous = generated
                        generated = await stages.measure(
                            "repair",
                            partial(
                                self.provider.repair,
                                blueprint,
                                previous,
                                issues,
                                attempt=repairs,
                            ),
                        )
                if browser_bundle is None:
                    raise RuntimeError("validated app build has no browser bundle")
                artifact = await stages.measure(
                    "package",
                    lambda: _as_awaitable(
                        self._package(
                            blueprint,
                            generated,
                            browser_bundle,
                            smoke_result,
                        )
                    ),
                )
        except TimeoutError as exc:
            raise TimeoutError(
                f"app build exceeded the {self.timeout_seconds:g}s worker deadline"
            ) from exc
        finally:
            if screenshot_dir is not None:
                shutil.rmtree(screenshot_dir, ignore_errors=True)

        completed_at = datetime.now(UTC)
        duration_ms = max(0, round((monotonic() - started) * 1000))
        metrics = BuildMetrics(
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            duration_ms=duration_ms,
            target_duration_ms=self.target_duration_ms,
            within_target=duration_ms < self.target_duration_ms,
            generation_attempts=attempts,
            repair_attempts=repairs,
            source_files=len(artifact.files),
            source_bytes=sum(len(source_file.contents.encode()) for source_file in artifact.files),
            stage_duration_ms=MappingProxyType(dict(stages.durations)),
        )
        return BuildCompletion(
            job_id=claim.job_id,
            app_id=claim.app_id,
            revision_id=claim.revision_id,
            artifact=artifact,
            metrics=metrics,
        )

    async def _review_visual_quality(
        self,
        stages: _StageTimer,
        screenshot_jpeg: bytes,
        blueprint: AppBlueprint,
        claim: BuildClaim,
    ) -> tuple[ValidationIssue, ...]:
        try:
            issues = await stages.measure(
                "visual_review",
                partial(
                    self.visual_reviewer.review,
                    screenshot_jpeg,
                    blueprint=blueprint,
                ),
            )
            return tuple(issues)
        except Exception:
            # The review gate guards aesthetics only. A reviewer outage must never block a
            # behaviorally verified app, so failures accept the candidate with a warning.
            logger.warning(
                "Visual review failed for job_id=%s; accepting the candidate without a score",
                claim.job_id,
                exc_info=True,
            )
            return ()

    def _package(
        self,
        blueprint: AppBlueprint,
        generated: GeneratedSource,
        browser_bundle: BrowserBundle,
        smoke_result: MappingProxyType[str, object] | None,
    ) -> BuildArtifact:
        smoke_values = dict(smoke_result or {})
        smoke_ready = smoke_values.get("ready") is True
        static_html_value = smoke_values.pop("static_html", browser_bundle.static_html)
        static_html = static_html_value if isinstance(static_html_value, str) else ""
        if self.require_browser_smoke and not smoke_ready:
            raise BuildRejectedError(
                (
                    ValidationIssue(
                        "missing_static_render",
                        "required isolated app render did not complete",
                    ),
                )
            )
        if smoke_ready and not static_html.strip():
            raise BuildRejectedError(
                (
                    ValidationIssue(
                        "missing_static_render",
                        "generated app did not produce usable isolated HTML",
                    ),
                )
            )
        if len(static_html.encode()) > MAX_STATIC_HTML_BYTES:
            raise BuildRejectedError(
                (
                    ValidationIssue(
                        "static_render_too_large",
                        "isolated app HTML exceeds 256 KB",
                    ),
                )
            )
        browser_bundle = BrowserBundle(
            format=browser_bundle.format,
            javascript=browser_bundle.javascript,
            css=browser_bundle.css,
            sha256=browser_bundle.sha256,
            sdk_version=browser_bundle.sdk_version,
            static_html=static_html,
            compiler=browser_bundle.compiler,
        )
        ordered_files = tuple(sorted(generated.files, key=lambda item: item.path))
        source_payload = [item.as_dict() for item in ordered_files]
        source_digest = content_hash(source_payload)
        smoke_report = {
            **smoke_values,
            "status": "passed" if smoke_ready else "skipped",
        }
        test_results = {
            "policy": {"status": "passed"},
            "typescript_compile": {
                "status": "passed",
                "compiler": dict(browser_bundle.compiler),
            },
            # static_html belongs only in browser_bundle. Duplicating it here would break
            # the durable test-result and artifact size envelopes for otherwise valid apps.
            "browser_smoke": smoke_report,
        }
        if len(canonical_json(test_results).encode()) > _MAX_TEST_RESULTS_BYTES:
            raise BuildRejectedError(
                (
                    ValidationIssue(
                        "build_test_results_too_large",
                        "build test metadata exceeds 128 KB",
                    ),
                )
            )
        unsigned = {
            "format_version": 1,
            "provider": self.provider.name,
            "provider_version": self.provider.version,
            "sdk_version": DOT_APP_SDK_VERSION,
            "entrypoint": generated.entrypoint,
            "files": source_payload,
            "manifest": dict(blueprint.manifest),
            "dependency_lock": dict(DOT_APP_DEPENDENCY_LOCK),
            "test_results": test_results,
            "source_hash": source_digest,
            "browser_bundle": browser_bundle.as_dict(),
        }
        artifact = BuildArtifact(
            format_version=1,
            provider=self.provider.name,
            provider_version=self.provider.version,
            sdk_version=DOT_APP_SDK_VERSION,
            entrypoint=generated.entrypoint,
            files=ordered_files,
            manifest=MappingProxyType(dict(blueprint.manifest)),
            dependency_lock=DOT_APP_DEPENDENCY_LOCK,
            test_results=MappingProxyType(test_results),
            source_hash=source_digest,
            content_hash=content_hash(unsigned),
            provider_metadata=MappingProxyType(dict(generated.provider_metadata)),
            browser_bundle=browser_bundle,
        )
        if len(canonical_json(artifact.as_dict()).encode()) > _MAX_BUILD_ARTIFACT_BYTES:
            raise BuildRejectedError(
                (
                    ValidationIssue(
                        "build_artifact_too_large",
                        "packaged app exceeds the 4 MB persistence envelope",
                    ),
                )
            )
        return artifact


async def _as_awaitable[T](value: T) -> T:
    return value


def _dedupe_issues(issues: tuple[ValidationIssue, ...]) -> tuple[ValidationIssue, ...]:
    return tuple(
        {
            (issue.code, issue.message, issue.path): issue
            for issue in issues
        }.values()
    )


def _acceptance_issue_fingerprint(
    issues: tuple[ValidationIssue, ...],
) -> tuple[tuple[str, str, str], ...] | None:
    acceptance = sorted(
        (issue.code, _acceptance_issue_scope(issue), issue.path or "")
        for issue in issues
        if issue.code.startswith(_ACCEPTANCE_ISSUE_PREFIX)
    )
    return tuple(acceptance) or None


def _acceptance_issue_scope(issue: ValidationIssue) -> str:
    """Keep fingerprints stable while distinguishing progress across entity workflows."""

    message = " ".join(issue.message.lower().split())
    for pattern in (
        r"expected operation=[a-z0-9_.-]+ entity=([a-z0-9_-]+)",
        r"declare operation=[a-z0-9_.-]+ entity=([a-z0-9_-]+)",
        r"invalid payload for operation=[a-z0-9_.-]+ entity=([a-z0-9_-]+)",
        r"no usable ([a-z0-9_-]+) create workflow",
        r"identify one ([a-z0-9_-]+) form",
        r"declared ([a-z0-9_-]+) fields",
        r"discovered ([a-z0-9_-]+) form",
        r"expected records\.(?:create|update|delete) for ([a-z0-9_-]+)",
        r"\"entity\":\"([a-z0-9_-]+)\"",
        r"^([a-z0-9_-]+) (?:was saved|refreshed after save)",
        r"name=\"([a-z0-9_-]+)\"",
        r"for ([a-z0-9_.-]+)(?:\s|$)",
        r"call ([a-z0-9_.-]+)(?:;|\s|$)",
    ):
        match = re.search(pattern, message)
        if match is not None:
            return match.group(1)
    if issue.code == "acceptance_reveal_mutation":
        return "workflow_reveal"
    # Dynamic selectors, captured runtime state, and body text should not make the same failure
    # look new on every candidate. The code remains the authoritative fallback scope.
    return issue.code


def _accumulated_diagnostics(
    history: list[tuple[ValidationIssue, ...]],
) -> tuple[ValidationIssue, ...]:
    # Keep the clean-regeneration prompt bounded while preserving every distinct diagnostic.
    flattened = tuple(issue for candidate in history for issue in candidate)
    return _dedupe_issues(flattened)[:30]


async def _regenerate_with_diagnostics(
    provider: AppSourceProvider,
    blueprint: AppBlueprint,
    issues: tuple[ValidationIssue, ...],
    *,
    attempt: int,
) -> GeneratedSource:
    regenerate: Any = getattr(provider, "regenerate", None)
    if callable(regenerate):
        return await regenerate(blueprint, issues, attempt=attempt)
    # Older/custom providers remain compatible. This is still a clean source generation rather
    # than another local patch, although only providers with `regenerate` receive diagnostics.
    return await provider.generate(blueprint)


def _safe_issue_log_payload(
    issues: tuple[ValidationIssue, ...],
) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for issue in issues:
        item = {
            "code": " ".join(issue.code.split())[:80],
            "message": " ".join(issue.message.split())[:800],
        }
        if issue.path is not None:
            item["path"] = " ".join(issue.path.split())[:240]
        payload.append(item)
    return payload


def _acceptance_plan(
    blueprint: AppBlueprint,
) -> tuple[MappingProxyType[str, object], ...]:
    """Derive small trusted interaction checks from the app's declared contract.

    Generated source does not own test instrumentation. The manifest tells Chromium which fields
    and runtime action to exercise, and the browser discovers the corresponding real form from its
    controls. Manifest order is dependency order, not UI hierarchy. Every declared persisted entity
    must have a working create workflow; earlier records are created first so later selects and
    relationships can use them.
    """
    steps: list[MappingProxyType[str, object]] = []
    entities = blueprint.manifest.get("entities", [])
    entity_definitions = [
        definition
        for definition in entities if isinstance(entities, list)
        if isinstance(definition, dict) and isinstance(definition.get("name"), str)
    ]
    entity_steps: list[tuple[MappingProxyType[str, object], MappingProxyType[str, object]]] = []
    for definition in entity_definitions:
        entity = definition["name"]
        fields = definition.get("fields", {})
        normalized_fields = (
            fields
            if isinstance(fields, dict)
            else {
                item["name"]: item
                for item in fields
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            if isinstance(fields, list)
            else {}
        )
        required_fields = sorted(
            name
            for name, field in normalized_fields.items()
            if (
                name not in _COMPUTED_ACCEPTANCE_FIELDS
                and isinstance(field, dict)
                and field.get("required") is True
            )
        )
        field_values = {
            name: _acceptance_value(field.get("type"))
            for name, field in normalized_fields.items()
            if name not in _COMPUTED_ACCEPTANCE_FIELDS and isinstance(field, dict)
        }
        list_step = MappingProxyType(
            {
                "operation": "records.list",
                "entity": entity,
                "required": False,
            }
        )
        create_step = MappingProxyType(
            {
                "operation": "records.create",
                "entity": entity,
                "required": True,
                "event_type": "submit",
                # These are best-effort browser interaction hints, not a required mapping
                # between database fields and DOM input names. Purpose-built controls can
                # derive persisted values; Chromium validates the submitted payload instead.
                "field_hints": field_values,
                "required_payload_fields": required_fields,
                "allowed_payload_fields": sorted(normalized_fields),
            }
        )
        entity_steps.append((list_step, create_step))
    steps.extend(item[0] for item in entity_steps)
    # Manifest order is dependency order. Exercise it in that order so a participant or parent
    # record exists before later forms need it for selects, splits, or relationships.
    steps.extend(item[1] for item in entity_steps)
    return tuple(steps)


def _acceptance_value(kind: object) -> object:
    return {
        "number": 1,
        "integer": 1,
        "money": 1,
        "boolean": True,
        "date": "2026-01-01",
        "datetime": "2026-01-01T09:00:00Z",
        "object": {},
        "array": [],
    }.get(kind, "test")


async def process_next_build(hooks: BuildJobHooks, pipeline: AppBuildPipeline) -> bool:
    """Claim and settle one job. Service hooks own transactions and completion events."""

    claim = await hooks.claim_next_build()
    if claim is None:
        return False
    logger.info(
        "Generated app build started job_id=%s app_id=%s queue_attempt=%s provider=%s",
        claim.job_id,
        claim.app_id,
        claim.attempt,
        pipeline.provider.name,
    )
    started = monotonic()
    try:
        completion = await pipeline.build(claim)
    except BlueprintValidationError as exc:
        await _fail_build_safely(
            hooks,
            claim,
            BuildFailure(
                job_id=claim.job_id,
                app_id=claim.app_id,
                revision_id=claim.revision_id,
                code="invalid_blueprint",
                message=str(exc),
                retryable=False,
                duration_ms=max(0, round((monotonic() - started) * 1000)),
            ),
        )
    except BuildRejectedError as exc:
        await _fail_build_safely(
            hooks,
            claim,
            BuildFailure(
                job_id=claim.job_id,
                app_id=claim.app_id,
                revision_id=claim.revision_id,
                code="source_rejected",
                message=str(exc),
                # The bounded pipeline already tried local repair plus one clean regeneration
                # when acceptance stopped converging. Requeueing the same blueprint only repeats
                # that model spend; timeouts and provider/infrastructure failures remain retryable.
                retryable=False,
                issues=exc.issues,
                duration_ms=max(0, round((monotonic() - started) * 1000)),
            ),
        )
    except TimeoutError as exc:
        await _fail_build_safely(
            hooks,
            claim,
            BuildFailure(
                job_id=claim.job_id,
                app_id=claim.app_id,
                revision_id=claim.revision_id,
                code="build_timeout",
                message=str(exc),
                retryable=True,
                duration_ms=max(0, round((monotonic() - started) * 1000)),
            ),
        )
    except Exception as exc:  # Worker boundary: unexpected provider failures must settle the claim.
        await _fail_build_safely(
            hooks,
            claim,
            BuildFailure(
                job_id=claim.job_id,
                app_id=claim.app_id,
                revision_id=claim.revision_id,
                code="provider_error",
                message=f"{type(exc).__name__}: {exc}",
                retryable=True,
                duration_ms=max(0, round((monotonic() - started) * 1000)),
            ),
        )
    else:
        try:
            await hooks.complete_build(claim, completion)
        except BuildCompletionHookError as exc:
            await _fail_build_safely(
                hooks,
                claim,
                BuildFailure(
                    job_id=claim.job_id,
                    app_id=claim.app_id,
                    revision_id=claim.revision_id,
                    code=exc.code,
                    message=str(exc),
                    retryable=exc.retryable,
                    duration_ms=max(0, round((monotonic() - started) * 1000)),
                ),
            )
        except Exception as exc:
            # Hooks are infrastructure adapters. A broken completion adapter must not take
            # down the durable worker or leave its claim untouched until process restart.
            await _fail_build_safely(
                hooks,
                claim,
                BuildFailure(
                    job_id=claim.job_id,
                    app_id=claim.app_id,
                    revision_id=claim.revision_id,
                    code="completion_hook_error",
                    message=f"{type(exc).__name__}: {exc}",
                    retryable=True,
                    duration_ms=max(0, round((monotonic() - started) * 1000)),
                ),
            )
        else:
            logger.info(
                "Generated app build completed job_id=%s app_id=%s duration_ms=%s "
                "generation_attempts=%s repair_attempts=%s",
                claim.job_id,
                claim.app_id,
                completion.metrics.duration_ms,
                completion.metrics.generation_attempts,
                completion.metrics.repair_attempts,
            )
    return True


async def _fail_build_safely(
    hooks: BuildJobHooks,
    claim: BuildClaim,
    failure: BuildFailure,
) -> None:
    logger.warning(
        "Generated app build failed job_id=%s app_id=%s code=%s retryable=%s issues=%s",
        claim.job_id,
        claim.app_id,
        failure.code,
        failure.retryable,
        _safe_issue_log_payload(failure.issues),
    )
    try:
        await hooks.fail_build(claim, failure)
    except Exception:
        # A database outage or a lost lease can also prevent failure settlement. The lease
        # remains recoverable by another poller; most importantly, this worker stays alive.
        logger.exception(
            "Could not settle generated-app build job=%s after %s",
            claim.job_id,
            failure.code,
        )
