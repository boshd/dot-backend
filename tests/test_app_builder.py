import asyncio
import json
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.app_builder.pipeline import (
    AppBuildPipeline,
    BuildRejectedError,
    _acceptance_plan,
    process_next_build,
)
from benji_api.app_builder.policy import inspect_generated_source
from benji_api.app_builder.providers import DeterministicLocalProvider, OpenAIAppSourceProvider
from benji_api.app_builder.service_hooks import GeneratedAppBuildServiceHooks
from benji_api.app_builder.types import (
    AppBlueprint,
    BlueprintValidationError,
    BrowserBundle,
    BuildClaim,
    BuildCompletion,
    BuildCompletionHookError,
    BuildFailure,
    GeneratedSource,
    SourceFile,
    ValidationIssue,
)
from benji_api.config import Settings
from benji_api.db.base import Base
from benji_api.models.channel import Conversation, ConversationMember
from benji_api.models.generated_app import GeneratedApp
from benji_api.models.generated_app_v2 import (
    GeneratedAppAccessTicket,
    GeneratedAppBuildJob,
    GeneratedAppBuildStatus,
    GeneratedAppDeployment,
    GeneratedAppRevision,
    GeneratedAppRole,
)
from benji_api.models.user import User
from benji_api.models.user_event import UserEvent
from benji_api.services.generated_apps_v2 import create_code_app_build, redeem_access_ticket


def blueprint() -> dict[str, object]:
    return {
        "title": "Cottage weekend",
        "description": "Keep the trip organized without a spreadsheet.",
        "purpose": "Plan the trip and settle shared expenses.",
        "layout": "trip_planner",
        "accent": "#e7654b",
        "manifest": {
            "schema_version": 1,
            "entities": [{"name": "expense", "fields": [{"name": "amount", "type": "money"}]}],
        },
        "seed_data": {"currency": "CAD"},
    }


def claim(**changes: object) -> BuildClaim:
    value = BuildClaim(
        job_id="job-1",
        app_id="app-1",
        revision_id="revision-1",
        blueprint=blueprint(),
    )
    return replace(value, **changes)


def test_blueprint_lengths_match_the_app_creation_contract() -> None:
    value = blueprint()
    value["title"] = "t" * 120
    value["description"] = "d" * 500

    parsed = AppBlueprint.from_mapping(value)

    assert len(parsed.title) == 120
    assert len(parsed.description) == 500


class FakeHooks:
    def __init__(self, next_claim: BuildClaim | None) -> None:
        self.next_claim = next_claim
        self.completed: list[BuildCompletion] = []
        self.failed: list[BuildFailure] = []

    async def claim_next_build(self) -> BuildClaim | None:
        value = self.next_claim
        self.next_claim = None
        return value

    async def complete_build(self, claim: BuildClaim, completion: BuildCompletion) -> None:
        assert claim.job_id == completion.job_id
        self.completed.append(completion)

    async def fail_build(self, claim: BuildClaim, failure: BuildFailure) -> None:
        assert claim.job_id == failure.job_id
        self.failed.append(failure)


class CompletionErrorHooks(FakeHooks):
    def __init__(self, next_claim: BuildClaim, *, fail_settlement: bool = False) -> None:
        super().__init__(next_claim)
        self.fail_settlement = fail_settlement

    async def complete_build(self, claim: BuildClaim, completion: BuildCompletion) -> None:
        del claim, completion
        raise BuildCompletionHookError(
            code="completion_conflict",
            message="revision changed",
            retryable=False,
        )

    async def fail_build(self, claim: BuildClaim, failure: BuildFailure) -> None:
        if self.fail_settlement:
            raise RuntimeError("database is unavailable")
        await super().fail_build(claim, failure)


class RepairingProvider:
    name = "repairing"
    version = "test"

    def __init__(self) -> None:
        self.local = DeterministicLocalProvider()
        self.repairs = 0

    async def generate(self, app_blueprint: AppBlueprint) -> GeneratedSource:
        safe = await self.local.generate(app_blueprint)
        unsafe = replace(
            safe.files[0],
            contents=f"{safe.files[0].contents}\nfetch('/steal');\n",
        )
        return replace(safe, files=(unsafe,))

    async def repair(
        self,
        app_blueprint: AppBlueprint,
        previous: GeneratedSource,
        issues: tuple[ValidationIssue, ...],
        *,
        attempt: int,
    ) -> GeneratedSource:
        del previous
        assert any(issue.code == "network_access" for issue in issues)
        assert attempt == 1
        self.repairs += 1
        return await self.local.generate(app_blueprint)


class NeverRepairsProvider(RepairingProvider):
    async def repair(
        self,
        app_blueprint: AppBlueprint,
        previous: GeneratedSource,
        issues: tuple[ValidationIssue, ...],
        *,
        attempt: int,
    ) -> GeneratedSource:
        del app_blueprint, issues, attempt
        self.repairs += 1
        return previous


class SlowProvider(DeterministicLocalProvider):
    async def generate(self, app_blueprint: AppBlueprint) -> GeneratedSource:
        await asyncio.sleep(0.02)
        return await super().generate(app_blueprint)


class FakeSmokeRunner:
    name = "fake-browser"
    version = "test"

    async def smoke(
        self,
        bundle: object,
        *,
        acceptance_plan: tuple[object, ...] = (),
    ) -> MappingProxyType[str, object]:
        del bundle, acceptance_plan
        return MappingProxyType(
            {
                "ready": True,
                "runtime_errors": 0,
                "static_html": '<main data-testid="app">ready</main>',
            }
        )


class MissingStaticSmokeRunner(FakeSmokeRunner):
    async def smoke(
        self,
        bundle: object,
        *,
        acceptance_plan: tuple[object, ...] = (),
    ) -> MappingProxyType[str, object]:
        del bundle, acceptance_plan
        return MappingProxyType({"ready": True, "runtime_errors": 0})


class OversizedArtifactCompiler:
    name = "oversized"
    version = "test"

    async def compile(self, source: GeneratedSource) -> BrowserBundle:
        del source
        return BrowserBundle(
            format="iife",
            javascript="x" * 4_000_000,
            css="",
            sha256="a" * 64,
            sdk_version="1",
        )


class FakeResponses:
    def __init__(self, output: dict[str, object]) -> None:
        self.output = output
        self.requests: list[dict[str, object]] = []

    async def create(self, **request: object) -> SimpleNamespace:
        self.requests.append(request)
        return SimpleNamespace(
            id="response-1",
            model="gpt-5.6-terra",
            output_text=json.dumps(self.output),
            usage=SimpleNamespace(input_tokens=120, output_tokens=340, total_tokens=460),
        )


class FakeOpenAIClient:
    def __init__(self, output: dict[str, object]) -> None:
        self.responses = FakeResponses(output)


def test_blueprint_validates_authority_contract() -> None:
    parsed = AppBlueprint.from_mapping(blueprint())
    assert parsed.title == "Cottage weekend"
    assert parsed.layout == "trip_planner"
    assert parsed.manifest["schema_version"] == 1

    with pytest.raises(BlueprintValidationError, match="accent"):
        AppBlueprint.from_mapping({**blueprint(), "accent": "orange"})
    with pytest.raises(BlueprintValidationError, match="unsupported capability"):
        AppBlueprint.from_mapping(
            {
                **blueprint(),
                "manifest": {
                    "schema_version": 1,
                    "capabilities": ["dot.message.send"],
                    "entities": [],
                },
            }
        )


@pytest.mark.anyio
async def test_local_build_is_deterministic_and_instrumented() -> None:
    pipeline = AppBuildPipeline(DeterministicLocalProvider())

    first = await pipeline.build(claim())
    second = await pipeline.build(claim())

    assert first.artifact.content_hash == second.artifact.content_hash
    assert first.artifact.source_hash == second.artifact.source_hash
    assert first.artifact.render_document["schema_version"] == 1
    assert first.artifact.render_document["root"]["type"] == "page"
    assert "export default App" in first.artifact.files[0].contents
    assert first.metrics.within_target is True
    assert first.metrics.repair_attempts == 0
    assert set(first.metrics.stage_duration_ms) == {
        "validate",
        "generate",
        "inspect",
        "compile",
        "package",
    }


@pytest.mark.anyio
async def test_required_smoke_render_is_stored_once_and_must_not_be_empty() -> None:
    completion = await AppBuildPipeline(
        DeterministicLocalProvider(),
        smoke_runner=FakeSmokeRunner(),
        require_browser_smoke=True,
    ).build(claim())

    assert completion.artifact.browser_bundle.static_html == (
        '<main data-testid="app">ready</main>'
    )
    assert "static_html" not in completion.artifact.test_results["browser_smoke"]

    with pytest.raises(BuildRejectedError) as rejected:
        await AppBuildPipeline(
            DeterministicLocalProvider(),
            max_repair_attempts=0,
            smoke_runner=MissingStaticSmokeRunner(),
            require_browser_smoke=True,
        ).build(claim())
    assert rejected.value.issues[0].code == "missing_static_render"


@pytest.mark.anyio
async def test_pipeline_rejects_artifact_before_persistence_size_limit() -> None:
    with pytest.raises(BuildRejectedError) as rejected:
        await AppBuildPipeline(
            DeterministicLocalProvider(),
            max_repair_attempts=0,
            compiler=OversizedArtifactCompiler(),
        ).build(claim())

    assert rejected.value.issues[0].code == "build_artifact_too_large"


@pytest.mark.anyio
async def test_policy_failure_gets_one_bounded_repair() -> None:
    provider = RepairingProvider()
    completion = await AppBuildPipeline(provider, max_repair_attempts=1).build(claim())

    assert provider.repairs == 1
    assert completion.metrics.generation_attempts == 2
    assert completion.metrics.repair_attempts == 1
    assert completion.artifact.provider == "repairing"


@pytest.mark.anyio
async def test_openai_provider_captures_model_tokens_and_latency() -> None:
    local = await DeterministicLocalProvider().generate(AppBlueprint.from_mapping(blueprint()))
    client = FakeOpenAIClient(
        {
            "files": [
                *[source_file.as_dict() for source_file in local.files],
                {
                    "path": "src/app.css",
                    "contents": (
                        '@import url("https://fonts.example.test/dot.css");\n'
                        '.hero { background-image: url("https://images.example.test/hero.png"); }'
                    ),
                },
                {
                    "path": "src/View.tsx",
                    "contents": (
                        'import "./missing.css";\n'
                        "export function View(): JSX.Element { "
                        "const [name, setName] = React.useState(''); "
                        "return <Input value={name} onChange={setName} />; }"
                    ),
                },
            ],
            "entrypoint": local.entrypoint,
        }
    )
    provider = OpenAIAppSourceProvider(client=client, model="gpt-5.6-terra")

    result = await provider.generate(AppBlueprint.from_mapping(blueprint()))

    assert result.provider_metadata["model"] == "gpt-5.6-terra"
    assert result.provider_metadata["response_id"] == "response-1"
    assert result.provider_metadata["token_usage"] == {
        "input_tokens": 120,
        "output_tokens": 340,
        "total_tokens": 460,
    }
    assert isinstance(result.provider_metadata["latency_ms"], int)
    assert result.provider_metadata["removed_external_css_assets"] == 2
    assert result.provider_metadata["normalized_react_types"] == 1
    assert result.provider_metadata["normalized_value_handlers"] == 1
    assert result.provider_metadata["canonicalized_css_imports"] == 2
    files = {item.path: item.contents for item in result.files}
    assert "src/dot-generated.css" not in files
    assert "src/app.css" not in files
    assert 'import "./missing.css";' not in files["src/View.tsx"]
    assert "React.ReactElement" in files["src/View.tsx"]
    assert "onValueChange={setName}" in files["src/View.tsx"]
    request = client.responses.requests[0]
    assert request["store"] is False
    assert request["max_output_tokens"] == 16_000
    assert "PrimaryWorkflowTrigger" in request["instructions"]
    assert "never write `data-dot-primary-action`" in request["instructions"]
    assert 'options={[{ value: "high", label: "High" }]}' in request["instructions"]
    assert "onValueChange={setName}" in request["instructions"]
    assert "onCheckedChange={setDone}" in request["instructions"]
    assert '<SegmentedControl label="Filter" value={filter}' in request["instructions"]
    assert '<ListItem title="Task" detail="Today" />' in request["instructions"]
    normalized_instructions = " ".join(request["instructions"].lower().split())
    assert "do not return css files, classname props, inline style props" in (
        normalized_instructions
    )


def test_source_policy_blocks_privileged_browser_and_dependency_access() -> None:
    document = {
        "schema_version": 1,
        "data": {},
        "root": {"id": "root", "type": "page", "children": []},
    }
    source = GeneratedSource(
        files=(
            SourceFile(
                "src/App.tsx",
                'import x from "unknown-package";\nfetch("/private");\nexport default x;',
            ),
        ),
        entrypoint="src/App.tsx",
        render_document=MappingProxyType(document),
    )

    codes = {issue.code for issue in inspect_generated_source(source)}
    assert {"dependency_not_allowed", "network_access"} <= codes


def test_source_policy_allows_typescript_comments_but_blocks_real_external_urls() -> None:
    document = {
        "schema_version": 1,
        "data": {},
        "root": {"id": "root", "type": "page", "children": []},
    }
    commented = GeneratedSource(
        files=(
            SourceFile(
                "src/App.tsx",
                "// Explain the next line.\nexport default function App() { return <p>ok</p>; }",
            ),
        ),
        entrypoint="src/App.tsx",
        render_document=MappingProxyType(document),
    )
    linked = replace(
        commented,
        files=(
            SourceFile(
                "src/App.tsx",
                (
                    'const help = "https://example.test"; '
                    "export default function App() { return <p>{help}</p>; }"
                ),
            ),
        ),
    )

    assert not any(issue.code == "external_url" for issue in inspect_generated_source(commented))
    assert any(issue.code == "external_url" for issue in inspect_generated_source(linked))


def test_source_policy_enforces_the_dot_design_boundary() -> None:
    source = GeneratedSource(
        files=(
            SourceFile(
                "src/App.tsx",
                '''import { AppShell, Card } from "@dot/ui";
export default function App() {
  return <AppShell title="votes"><Card className="neon" style={{ color: "#fff" }}>
    <button>vote</button><svg fill="hotpink" />
  </Card><style>{`.neon { background: rgb(255 0 0); }`}</style></AppShell>;
}''',
            ),
            SourceFile("src/app.css", ".neon { font-size: 100px; }"),
        ),
        entrypoint="src/App.tsx",
        render_document=MappingProxyType(
            {
                "schema_version": 1,
                "theme": {"accent": "coral"},
                "data": {},
                "root": {"id": "app", "type": "page", "children": []},
            }
        ),
    )

    codes = {issue.code for issue in inspect_generated_source(source)}

    assert {
        "custom_class_name",
        "inline_style",
        "embedded_style_element",
        "unbranded_interactive_control",
        "raw_color_value",
        "raw_color_attribute",
        "generated_css_not_allowed",
    } <= codes


def test_source_policy_accepts_semantic_dot_ui_and_chart_tokens() -> None:
    source = GeneratedSource(
        files=(
            SourceFile(
                "src/App.tsx",
                '''import { AppShell, Button, Card, Input, Stack, chartTokens } from "@dot/ui";
export default function App() {
  return <AppShell title="votes" accent="sky"><Stack><Card>
    <Input label="your vote" name="choice" />
    <Button>vote</Button>
    <svg><path stroke={chartTokens.primary} fill="none" /></svg>
  </Card></Stack></AppShell>;
}''',
            ),
        ),
        entrypoint="src/App.tsx",
        render_document=MappingProxyType(
            {
                "schema_version": 1,
                "theme": {"accent": "sky"},
                "data": {},
                "root": {"id": "app", "type": "page", "children": []},
            }
        ),
    )

    assert not inspect_generated_source(source)


def test_source_policy_rejects_sdk_reserved_primary_action_marker() -> None:
    source = GeneratedSource(
        files=(
            SourceFile(
                "src/App.tsx",
                '''import { Button } from "@dot/ui";
export default function App() {
  return <Button data-dot-primary-action>open</Button>;
}''',
            ),
        ),
        entrypoint="src/App.tsx",
        render_document=MappingProxyType(
            {
                "schema_version": 1,
                "data": {},
                "root": {"id": "app", "type": "page", "children": []},
            }
        ),
    )

    assert "reserved_primary_action_marker" in {
        issue.code for issue in inspect_generated_source(source)
    }


def test_render_policy_requires_unique_ids_and_managed_actions() -> None:
    source = GeneratedSource(
        files=(SourceFile("src/App.tsx", "export default function App() { return null; }"),),
        entrypoint="src/App.tsx",
        render_document=MappingProxyType(
            {
                "schema_version": 1,
                "root": {
                    "id": "same",
                    "type": "page",
                    "children": [
                        {
                            "id": "same",
                            "type": "button",
                            "action": {"operation": "dot.start", "payload": {}},
                            "children": [],
                        }
                    ],
                },
            }
        ),
    )

    codes = {issue.code for issue in inspect_generated_source(source)}
    assert {"duplicate_render_node_id", "invalid_render_action_operation"} <= codes


def test_render_policy_allows_reminder_only_with_manifest_grant() -> None:
    source = GeneratedSource(
        files=(SourceFile("src/App.tsx", "export default function App() { return null; }"),),
        entrypoint="src/App.tsx",
        render_document=MappingProxyType(
            {
                "schema_version": 1,
                "root": {
                    "id": "app",
                    "type": "page",
                    "children": [
                        {
                            "id": "reminder",
                            "type": "button",
                            "action": {
                                "operation": "dot.reminder.create",
                                "payload": {
                                    "title": "walk",
                                    "goal": "take a walk",
                                    "run_at": "2026-08-13T18:00:00+03:00",
                                    "timezone": "Africa/Cairo",
                                    "recurrence": "once",
                                },
                                "confirm": {"title": "Set this reminder?"},
                            },
                            "children": [],
                        }
                    ],
                },
            }
        ),
    )

    assert "invalid_render_action_operation" in {
        issue.code for issue in inspect_generated_source(source)
    }
    assert not inspect_generated_source(
        source,
        allowed_capabilities=frozenset({"dot.reminder.create"}),
    )


def test_render_policy_binds_record_actions_to_manifest_entities_and_fields() -> None:
    source = GeneratedSource(
        files=(SourceFile("src/App.tsx", "export default function App() { return null; }"),),
        entrypoint="src/App.tsx",
        render_document=MappingProxyType(
            {
                "schema_version": 1,
                "root": {
                    "id": "app",
                    "type": "page",
                    "children": [
                        {
                            "id": "save",
                            "type": "button",
                            "action": {
                                "operation": "records.create",
                                "payload": {
                                    "entity": "made_up",
                                    "data": {"surprise": True},
                                },
                            },
                            "children": [],
                        }
                    ],
                },
            }
        ),
    )

    issues = inspect_generated_source(
        source,
        manifest={
            "schema_version": 1,
            "entities": [{"name": "task", "fields": {"title": {"type": "string"}}}],
        },
    )

    assert {issue.code for issue in issues} == {"undeclared_record_action_entity"}


def test_acceptance_plan_requires_primary_create_even_with_supporting_entities() -> None:
    app_blueprint = AppBlueprint.from_mapping(
        {
            **blueprint(),
            "manifest": {
                "schema_version": 1,
                "entities": [
                    {
                        "name": "expense",
                        "fields": {
                            "amount": {"type": "number", "required": True},
                            "note": {"type": "string", "required": False},
                            "created_at": {"type": "datetime", "required": True},
                            "updated_at": {"type": "datetime", "required": True},
                            "id": {"type": "string", "required": True},
                            "version": {"type": "integer", "required": True},
                        },
                    },
                    {
                        "name": "participant",
                        "fields": {"name": {"type": "string", "required": True}},
                    },
                ],
            },
        }
    )
    generated = GeneratedSource(
        files=(SourceFile("src/App.tsx", "export default function App() { return null; }"),),
        entrypoint="src/App.tsx",
        render_document=MappingProxyType(
            {
                "schema_version": 1,
                "root": {
                    "id": "app",
                    "type": "page",
                    "children": [
                        {
                            "id": "expense_form",
                            "type": "form",
                            "action": {
                                "operation": "records.create",
                                "payload": {"entity": "expense", "data": {}},
                            },
                            "children": [],
                        }
                    ],
                },
            }
        ),
    )

    plan = [dict(step) for step in _acceptance_plan(app_blueprint, generated)]

    assert plan[0] == {
        "operation": "records.list",
        "entity": "expense",
        "required": False,
    }
    assert plan[1] == {
        "operation": "ui.reveal_primary",
        "required": False,
        "selector": "[data-dot-primary-action]",
        "event_type": "click",
    }
    assert plan[2]["selector"] == (
        '[data-dot-operation="records.create"][data-dot-entity="expense"]'
    )
    assert plan[2]["required"] is True
    assert plan[2]["event_type"] == "submit"
    assert plan[2]["field_hints"] == {"amount": 1, "note": "test"}
    assert plan[2]["required_payload_fields"] == ["amount"]
    assert plan[2]["allowed_payload_fields"] == [
        "amount",
        "created_at",
        "id",
        "note",
        "updated_at",
        "version",
    ]
    assert plan[4]["operation"] == "records.create"
    assert plan[4]["entity"] == "participant"
    assert plan[4]["required"] is False


@pytest.mark.anyio
async def test_worker_completes_and_fails_claims() -> None:
    successful = FakeHooks(claim())
    handled = await process_next_build(
        successful,
        AppBuildPipeline(DeterministicLocalProvider()),
    )
    assert handled is True
    assert len(successful.completed) == 1
    assert not successful.failed

    rejected = FakeHooks(claim())
    await process_next_build(
        rejected,
        AppBuildPipeline(NeverRepairsProvider(), max_repair_attempts=1),
    )
    assert not rejected.completed
    assert rejected.failed[0].code == "source_rejected"
    assert rejected.failed[0].retryable is False
    assert rejected.failed[0].issues[0].code in {"network_access", "external_url"}


@pytest.mark.anyio
async def test_worker_marks_timeout_retryable() -> None:
    hooks = FakeHooks(claim())
    await process_next_build(
        hooks,
        AppBuildPipeline(SlowProvider(), timeout_seconds=0.001),
    )
    assert hooks.failed[0].code == "build_timeout"
    assert hooks.failed[0].retryable is True


@pytest.mark.anyio
async def test_worker_returns_false_when_queue_is_empty() -> None:
    hooks = FakeHooks(None)
    assert not await process_next_build(
        hooks,
        AppBuildPipeline(DeterministicLocalProvider()),
    )


@pytest.mark.anyio
async def test_completion_hook_errors_are_settled_without_escaping_worker_boundary() -> None:
    hooks = CompletionErrorHooks(claim())

    assert await process_next_build(hooks, AppBuildPipeline(DeterministicLocalProvider()))
    assert hooks.failed[0].code == "completion_conflict"
    assert hooks.failed[0].retryable is False

    unavailable = CompletionErrorHooks(claim(), fail_settlement=True)
    assert await process_next_build(
        unavailable,
        AppBuildPipeline(DeterministicLocalProvider()),
    )


def test_completion_error_classification_bounds_retries() -> None:
    from sqlalchemy.exc import IntegrityError

    from benji_api.app_builder.service_hooks import _classify_completion_error
    from benji_api.services.generated_apps_v2 import (
        CodeAppConflictError,
        CodeAppValidationError,
    )

    assert _classify_completion_error(CodeAppValidationError("invalid artifact")) == (
        "completion_validation_error",
        False,
    )
    assert _classify_completion_error(CodeAppConflictError("revision changed")) == (
        "completion_conflict",
        False,
    )
    assert _classify_completion_error(
        IntegrityError("insert revision", {}, RuntimeError("duplicate"))
    ) == ("completion_conflict", False)
    assert _classify_completion_error(RuntimeError("database unavailable")) == (
        "completion_persistence_error",
        True,
    )


@pytest.mark.anyio
async def test_transient_completion_error_rolls_back_and_retries_then_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from benji_api.services import generated_apps_v2

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        user = User(phone_number="+14155550122")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        app, job, _ = await create_code_app_build(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            title="Cottage weekend",
            description="Plan and split the trip.",
            request={"blueprint": blueprint()},
            delivery_provider="linq",
        )

    durable_complete_build = generated_apps_v2.complete_build
    completion_attempts = 0

    async def flaky_complete_build(session: AsyncSession, **kwargs: object) -> object:
        nonlocal completion_attempts
        completion_attempts += 1
        if completion_attempts == 1:
            partial_app = await session.get(GeneratedApp, app.id)
            assert partial_app is not None
            partial_app.title = "partial promotion"
            await session.flush()
            raise RuntimeError("temporary persistence failure")
        return await durable_complete_build(session, **kwargs)

    monkeypatch.setattr(generated_apps_v2, "complete_build", flaky_complete_build)
    hooks = GeneratedAppBuildServiceHooks(
        worker_id="builder-retry",
        lease_seconds=60,
        session_factory=factory,
        settings=Settings(generated_app_public_url="https://app.textdot.test"),
    )
    pipeline = AppBuildPipeline(
        DeterministicLocalProvider(),
        smoke_runner=FakeSmokeRunner(),
        require_browser_smoke=True,
    )

    assert await process_next_build(hooks, pipeline)
    async with factory() as session:
        rolled_back_app = await session.get(GeneratedApp, app.id)
        queued_job = await session.get(GeneratedAppBuildJob, job.id)
        assert rolled_back_app is not None
        assert rolled_back_app.title == "Cottage weekend"
        assert queued_job is not None
        assert queued_job.status == GeneratedAppBuildStatus.QUEUED.value
        assert queued_job.result["failure_code"] == "completion_persistence_error"

    assert await process_next_build(hooks, pipeline)
    async with factory() as session:
        completed_job = await session.get(GeneratedAppBuildJob, job.id)
        assert completed_job is not None
        assert completed_job.status == GeneratedAppBuildStatus.SUCCEEDED.value
        assert completed_job.attempts == 2
    await engine.dispose()


@pytest.mark.anyio
async def test_durable_hooks_promote_revision_and_enqueue_completion_event() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        user = User(phone_number="+14155550123")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
        app, _, _ = await create_code_app_build(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            title="Cottage weekend",
            description="Plan and split the trip.",
            request={"blueprint": blueprint()},
            delivery_provider="linq",
        )

    hooks = GeneratedAppBuildServiceHooks(
        worker_id="builder-test",
        lease_seconds=60,
        session_factory=factory,
        settings=Settings(generated_app_public_url="https://app.textdot.test"),
    )
    assert await process_next_build(
        hooks,
        AppBuildPipeline(
            DeterministicLocalProvider(),
            smoke_runner=FakeSmokeRunner(),
            require_browser_smoke=True,
        ),
    )

    async with factory() as session:
        deployment = await session.get(GeneratedAppDeployment, app.id)
        assert deployment is not None
        revision = await session.get(GeneratedAppRevision, deployment.active_revision_id)
        assert revision is not None
        assert revision.artifact["render_document"]["schema_version"] == 1
        event = await session.scalar(
            select(UserEvent).where(UserEvent.event_type == "app.build.completed")
        )
        assert event is not None
        assert event.payload["app_url"].startswith(
            f"https://app.textdot.test/a/{app.public_id}#handoff="
        )
        handoff = urlsplit(event.payload["app_url"]).fragment.removeprefix("handoff=")
        session_token, app_session = await redeem_access_ticket(
            session,
            public_id=app.public_id,
            token=handoff,
        )
        assert session_token
        assert app_session.user_id == user.id
        assert app_session.role == GeneratedAppRole.OWNER.value
        assert event.payload["duration_ms"] >= 0
    await engine.dispose()


@pytest.mark.anyio
async def test_durable_hooks_issue_group_member_handoff_on_completion() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        user = User(phone_number="+14155550124")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id, kind="group", title="Cottage crew")
        session.add(conversation)
        await session.flush()
        session.add(
            ConversationMember(
                conversation_id=conversation.id,
                user_id=user.id,
                external_handle=user.phone_number,
                role="owner",
            )
        )
        app, _, _ = await create_code_app_build(
            session,
            user_id=user.id,
            conversation_id=conversation.id,
            title="Cottage weekend",
            description="Plan and split the trip.",
            request={"blueprint": blueprint()},
            delivery_provider="linq",
        )

    hooks = GeneratedAppBuildServiceHooks(
        worker_id="builder-group",
        session_factory=factory,
        settings=Settings(generated_app_public_url="https://app.textdot.test"),
    )
    assert await process_next_build(
        hooks,
        AppBuildPipeline(
            DeterministicLocalProvider(),
            smoke_runner=FakeSmokeRunner(),
            require_browser_smoke=True,
        ),
    )
    async with factory() as session:
        tickets = list(
            (
                await session.scalars(
                    select(GeneratedAppAccessTicket)
                    .where(GeneratedAppAccessTicket.app_id == app.id)
                    .order_by(GeneratedAppAccessTicket.created_at)
                )
            ).all()
        )
        assert len(tickets) == 2
        assert all(ticket.principal_user_id is None for ticket in tickets)
        assert all(ticket.role == GeneratedAppRole.MEMBER.value for ticket in tickets)
    await engine.dispose()
