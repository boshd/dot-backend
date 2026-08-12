from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from time import monotonic
from types import MappingProxyType

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
from benji_api.generated_app_contract import parse_generated_app_capabilities

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
    ) -> None:
        if max_repair_attempts < 0 or max_repair_attempts > 4:
            raise ValueError("max_repair_attempts must be between 0 and 4")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if require_browser_smoke and smoke_runner is None:
            raise ValueError("require_browser_smoke needs a configured smoke runner")
        self.provider = provider
        self.max_repair_attempts = max_repair_attempts
        self.timeout_seconds = timeout_seconds
        self.target_duration_ms = target_duration_ms
        self.compiler = compiler or EsbuildAppCompiler(timeout_seconds=min(20.0, timeout_seconds))
        self.smoke_runner = smoke_runner
        self.require_browser_smoke = require_browser_smoke

    async def build(self, claim: BuildClaim) -> BuildCompletion:
        started_at = datetime.now(UTC)
        started = monotonic()
        stages = _StageTimer({})
        attempts = 0
        repairs = 0
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
                            inspect_generated_source(
                                current,
                                allowed_capabilities=frozenset(
                                    parse_generated_app_capabilities(blueprint.manifest)
                                ),
                                manifest=blueprint.manifest,
                            )
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
                            try:
                                result = await stages.measure(
                                    "smoke",
                                    partial(
                                        self.smoke_runner.smoke,
                                        browser_bundle,
                                        acceptance_plan=_acceptance_plan(
                                            blueprint,
                                            generated,
                                        ),
                                    ),
                                )
                                smoke_result = MappingProxyType(dict(result))
                            except AppBrowserSmokeError as exc:
                                issues = exc.issues
                        else:
                            if self.require_browser_smoke:
                                raise RuntimeError("required browser smoke runner is unavailable")
                            smoke_result = MappingProxyType(
                                {"ready": None, "status": "not_configured"}
                            )
                    if not issues:
                        break
                    logger.info(
                        "Generated app candidate needs repair job_id=%s attempt=%s issues=%s",
                        claim.job_id,
                        attempts,
                        [issue.code for issue in issues],
                    )
                    if repairs >= self.max_repair_attempts:
                        raise BuildRejectedError(issues)
                    repairs += 1
                    attempts += 1
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
            "render_contract": {"status": "passed", "schema_version": 1},
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
            "render_document": dict(generated.render_document),
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
            render_document=MappingProxyType(dict(generated.render_document)),
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


def _acceptance_plan(
    blueprint: AppBlueprint,
    generated: GeneratedSource,
) -> tuple[MappingProxyType[str, object], ...]:
    """Derive small trusted interaction checks from the app's declared contract.

    The render document is not the primary runtime. The first manifest entity is the app's primary
    persisted workflow, so its create path must work even when the app declares supporting entities.
    A generated app may put that form behind one explicit primary-action control; the smoke harness
    clicks that control before looking for and submitting the form. Supporting entity creates remain
    best-effort because they can legitimately depend on records created earlier in the workflow.
    """

    document_actions: dict[str, list[tuple[dict[str, object], str]]] = {}

    def visit(value: object) -> None:
        if not isinstance(value, dict):
            return
        action = value.get("action")
        if isinstance(action, dict) and isinstance(action.get("operation"), str):
            operation = action["operation"]
            node_type = value.get("type") if isinstance(value.get("type"), str) else "button"
            document_actions.setdefault(operation, []).append((action, node_type))
        children = value.get("children")
        if isinstance(children, list):
            for child in children:
                visit(child)

    visit(generated.render_document.get("root"))
    steps: list[MappingProxyType[str, object]] = []
    entities = blueprint.manifest.get("entities", [])
    entity_definitions = [
        definition
        for definition in entities if isinstance(entities, list)
        if isinstance(definition, dict) and isinstance(definition.get("name"), str)
    ]
    for entity_index, definition in enumerate(entity_definitions):
        entity = definition["name"]
        is_primary = entity_index == 0
        action_entry = next(
            (
                candidate
                for candidate in document_actions.get("records.create", [])
                if isinstance(candidate[0].get("payload"), dict)
                and candidate[0]["payload"].get("entity") == entity
            ),
            None,
        )
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
        steps.append(
            MappingProxyType(
                {
                    "operation": "records.list",
                    "entity": entity,
                    "required": False,
                }
            )
        )
        if is_primary:
            steps.append(
                MappingProxyType(
                    {
                        "operation": "ui.reveal_primary",
                        "required": False,
                        "selector": "[data-dot-primary-action]",
                        "event_type": "click",
                    }
                )
            )
        if action_entry is None:
            node_type = "form"
        else:
            _, node_type = action_entry
        steps.append(
            MappingProxyType(
                {
                    "operation": "records.create",
                    "entity": entity,
                    "required": is_primary,
                    "selector": (
                        f'[data-dot-operation="records.create"][data-dot-entity="{entity}"]'
                    ),
                    "event_type": "submit" if node_type == "form" else "click",
                    # These are best-effort browser interaction hints, not a required mapping
                    # between database fields and DOM input names. Purpose-built controls can
                    # derive persisted values; Chromium validates the submitted payload instead.
                    "field_hints": field_values,
                    "required_payload_fields": required_fields,
                    "allowed_payload_fields": sorted(normalized_fields),
                }
            )
        )
    for capability in parse_generated_app_capabilities(blueprint.manifest):
        document_action = next(iter(document_actions.get(capability, [])), None)
        steps.append(
            MappingProxyType(
                {
                    "operation": capability,
                    "required": document_action is not None,
                    "selector": f'[data-dot-operation="{capability}"]',
                    "event_type": "click",
                }
            )
        )
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
                # A fresh generation can escape a bad local optimum after the bounded in-build
                # repair loop. The durable queue still caps total attempts.
                retryable=True,
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
        [issue.code for issue in failure.issues],
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
