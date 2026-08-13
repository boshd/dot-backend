import asyncio
import json
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.app_builder.browser_smoke import AppBrowserSmokeError
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


class LayeredFailureProvider(RepairingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.first_repair_codes: set[str] = set()

    async def generate(self, app_blueprint: AppBlueprint) -> GeneratedSource:
        safe = await self.local.generate(app_blueprint)
        broken = replace(
            safe.files[0],
            contents=safe.files[0].contents.replace(
                "<Card>",
                '<Card style={{ color: "red" }} unknownProp="nope">',
            ),
        )
        return replace(safe, files=(broken,))

    async def repair(
        self,
        app_blueprint: AppBlueprint,
        previous: GeneratedSource,
        issues: tuple[ValidationIssue, ...],
        *,
        attempt: int,
    ) -> GeneratedSource:
        del previous
        assert attempt == 1
        self.first_repair_codes = {issue.code for issue in issues}
        self.repairs += 1
        return await self.local.generate(app_blueprint)


class SlowProvider(DeterministicLocalProvider):
    async def generate(self, app_blueprint: AppBlueprint) -> GeneratedSource:
        await asyncio.sleep(0.02)
        return await super().generate(app_blueprint)


class UnavailableProvider(DeterministicLocalProvider):
    name = "unavailable"
    version = "test"

    async def generate(self, app_blueprint: AppBlueprint) -> GeneratedSource:
        del app_blueprint
        raise RuntimeError("sensitive upstream quota response")


class UnavailableRepairProvider(RepairingProvider):
    async def repair(
        self,
        app_blueprint: AppBlueprint,
        previous: GeneratedSource,
        issues: tuple[ValidationIssue, ...],
        *,
        attempt: int,
    ) -> GeneratedSource:
        del app_blueprint, previous, issues, attempt
        raise RuntimeError("sensitive repair provider response")


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
                "real_browser": {"ready": True, "runtime": "chromium"},
                "static_html": '<main data-testid="app">ready</main>',
            }
        )


class SequencedIssueSmokeRunner(FakeSmokeRunner):
    def __init__(self, outcomes: list[ValidationIssue | None]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def smoke(
        self,
        bundle: object,
        *,
        acceptance_plan: tuple[object, ...] = (),
    ) -> MappingProxyType[str, object]:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise AppBrowserSmokeError((outcome,))
        return await super().smoke(bundle, acceptance_plan=acceptance_plan)


class ConvergenceProvider(DeterministicLocalProvider):
    name = "convergence"
    version = "test"

    def __init__(self) -> None:
        self.repairs = 0
        self.regenerations = 0
        self.regeneration_diagnostics: tuple[ValidationIssue, ...] = ()

    async def repair(
        self,
        app_blueprint: AppBlueprint,
        previous: GeneratedSource,
        issues: tuple[ValidationIssue, ...],
        *,
        attempt: int,
    ) -> GeneratedSource:
        del previous, issues, attempt
        self.repairs += 1
        return await super().generate(app_blueprint)

    async def regenerate(
        self,
        app_blueprint: AppBlueprint,
        issues: tuple[ValidationIssue, ...],
        *,
        attempt: int,
    ) -> GeneratedSource:
        del attempt
        self.regenerations += 1
        self.regeneration_diagnostics = issues
        return await super().generate(app_blueprint)


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
    assert first.artifact.browser_bundle.javascript
    assert set(first.artifact.as_dict()) == {
        "format_version",
        "provider",
        "provider_version",
        "sdk_version",
        "entrypoint",
        "files",
        "manifest",
        "dependency_lock",
        "test_results",
        "source_hash",
        "content_hash",
        "provider_metadata",
        "browser_bundle",
    }
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
async def test_repair_receives_policy_and_typescript_issues_together() -> None:
    provider = LayeredFailureProvider()

    completion = await AppBuildPipeline(provider, max_repair_attempts=1).build(claim())

    assert completion.metrics.repair_attempts == 1
    assert "inline_style" in provider.first_repair_codes
    assert "typescript_type_error" in provider.first_repair_codes


@pytest.mark.anyio
async def test_repeated_acceptance_failure_forces_one_clean_regeneration() -> None:
    issue = ValidationIssue(
        "acceptance_flow_missing",
        "no visible submit form matched the declared workout fields: title",
        "workout",
    )
    evolved_diagnostic = ValidationIssue(
        "acceptance_flow_missing",
        "no visible submit form matched the declared workout fields: title, notes",
        "workout",
    )
    provider = ConvergenceProvider()
    smoke = SequencedIssueSmokeRunner([issue, evolved_diagnostic, None])

    completion = await AppBuildPipeline(
        provider,
        max_repair_attempts=4,
        smoke_runner=smoke,
    ).build(claim())

    assert provider.repairs == 1
    assert provider.regenerations == 1
    assert provider.regeneration_diagnostics == (issue, evolved_diagnostic)
    assert smoke.calls == 3
    assert completion.metrics.generation_attempts == 3
    assert completion.metrics.repair_attempts == 2


@pytest.mark.anyio
async def test_repeated_acceptance_after_clean_regeneration_stops_early() -> None:
    issue = ValidationIssue(
        "acceptance_flow_missing",
        "no visible submit form matched the declared workout fields",
    )
    provider = ConvergenceProvider()
    smoke = SequencedIssueSmokeRunner([issue, issue, issue, None, None])

    with pytest.raises(BuildRejectedError) as rejected:
        await AppBuildPipeline(
            provider,
            max_repair_attempts=4,
            smoke_runner=smoke,
        ).build(claim())

    assert rejected.value.issues == (issue,)
    assert provider.repairs == 1
    assert provider.regenerations == 1
    assert smoke.calls == 3


@pytest.mark.anyio
async def test_acceptance_progress_across_entities_is_not_mistaken_for_convergence() -> None:
    participant = ValidationIssue(
        "acceptance_flow_missing",
        "no usable participant create workflow was found; forms=[]",
    )
    workout = ValidationIssue(
        "acceptance_flow_missing",
        "no usable workout_session create workflow was found; forms=[]",
    )
    provider = ConvergenceProvider()
    smoke = SequencedIssueSmokeRunner([participant, workout, None])

    await AppBuildPipeline(
        provider,
        max_repair_attempts=4,
        smoke_runner=smoke,
    ).build(claim())

    assert provider.repairs == 2
    assert provider.regenerations == 0
    assert smoke.calls == 3


@pytest.mark.anyio
async def test_workflow_mismatch_progress_across_entities_is_not_convergence() -> None:
    participant = ValidationIssue(
        "acceptance_workflow_mismatch",
        "form produced the wrong workflow mutation; expected operation=records.create "
        "entity=participant, observed operation=records.create entity=recommendation",
    )
    expense = ValidationIssue(
        "acceptance_workflow_mismatch",
        "form produced the wrong workflow mutation; expected operation=records.create "
        "entity=expense, observed operation=records.create entity=participant",
    )
    provider = ConvergenceProvider()
    smoke = SequencedIssueSmokeRunner([participant, expense, None])

    await AppBuildPipeline(
        provider,
        max_repair_attempts=4,
        smoke_runner=smoke,
    ).build(claim())

    assert provider.repairs == 2
    assert provider.regenerations == 0
    assert smoke.calls == 3


@pytest.mark.anyio
async def test_exhausted_custom_build_is_rejected_instead_of_publishing_fallback() -> None:
    with pytest.raises(BuildRejectedError) as rejected:
        await AppBuildPipeline(
            NeverRepairsProvider(),
            max_repair_attempts=0,
        ).build(claim())

    assert "network_access" in {issue.code for issue in rejected.value.issues}


@pytest.mark.anyio
async def test_custom_build_timeout_does_not_publish_fallback() -> None:
    with pytest.raises(TimeoutError, match="worker deadline"):
        await AppBuildPipeline(
            SlowProvider(),
            timeout_seconds=0.001,
        ).build(claim())


@pytest.mark.anyio
async def test_source_provider_failure_does_not_publish_fallback() -> None:
    with pytest.raises(RuntimeError, match="sensitive upstream quota response"):
        await AppBuildPipeline(UnavailableProvider()).build(claim())


@pytest.mark.anyio
async def test_repair_provider_failure_does_not_publish_fallback() -> None:
    with pytest.raises(RuntimeError, match="sensitive repair provider response"):
        await AppBuildPipeline(
            UnavailableRepairProvider(),
            max_repair_attempts=1,
        ).build(claim())


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
    assert "dependency order, never visual priority" in request["input"][0]["content"]
    output_schema = request["text"]["format"]["schema"]
    source_path_schema = output_schema["properties"]["files"]["items"]["properties"]["path"]
    assert source_path_schema["pattern"].startswith("^src/")
    assert output_schema["properties"]["entrypoint"]["pattern"].startswith("^src/")
    assert "package manifests" in request["instructions"]
    assert "PrimaryWorkflowTrigger" in request["instructions"]
    assert "WorkflowForm" in request["instructions"]
    assert "PERSISTENCE AND USER ACTIONS" in request["instructions"]
    assert "PERSISTENCE AND ACCEPTANCE" not in request["instructions"]
    assert "data-dot-operation" not in request["instructions"]
    assert "data-dot-entity" not in request["instructions"]
    assert "data-dot-primary-action" not in request["instructions"]
    assert "Never add `data-dot-*` attributes yourself" in request["instructions"]
    assert "plain persisted user text" in request["instructions"]
    assert "never embed example or external URL literals" in request["instructions"]
    assert 'options={[{ value: "high", label: "High" }]}' in request["instructions"]
    assert "onValueChange={setName}" in request["instructions"]
    assert "onCheckedChange={setDone}" in request["instructions"]
    assert '<SegmentedControl label="Filter" value={filter}' in request["instructions"]
    assert '<ListItem title="Task" detail="Today" />' in request["instructions"]
    assert "AUTHORITATIVE @dot/app-runtime TYPES" in request["instructions"]
    assert "export declare function useAppData" in request["instructions"]
    assert "export declare function useRecords" in request["instructions"]
    assert "export declare function runAction" in request["instructions"]
    assert 'await runAction("records.create", { entity, data })' in request["instructions"]
    assert (
        'await runAction("records.update", { record_id, expected_version, data })'
        in request["instructions"]
    )
    assert (
        'await runAction("records.delete", { record_id, expected_version })'
        in request["instructions"]
    )
    assert 'await runAction("dot.reminder.create", {' in request["instructions"]
    assert "records.create({" not in request["instructions"]
    assert "records.update({" not in request["instructions"]
    assert "records.delete({" not in request["instructions"]
    assert "dot.reminder.create({" not in request["instructions"]
    assert "AUTHORITATIVE @dot/ui TYPES" in request["instructions"]
    assert "export declare function Input" in request["instructions"]
    assert "onValueChange?: (value: string) => void" in request["instructions"]
    normalized_instructions = " ".join(request["instructions"].lower().split())
    assert "do not return css files, classname props, inline style props" in (
        normalized_instructions
    )


@pytest.mark.anyio
async def test_openai_repair_prompts_explain_acceptance_and_clean_rebuild() -> None:
    app_blueprint = AppBlueprint.from_mapping(blueprint())
    local = await DeterministicLocalProvider().generate(app_blueprint)
    client = FakeOpenAIClient(
        {
            "files": [source_file.as_dict() for source_file in local.files],
            "entrypoint": local.entrypoint,
        }
    )
    provider = OpenAIAppSourceProvider(client=client)
    issues = (
        ValidationIssue(
            "acceptance_flow_missing",
            "no visible submit form matched the declared expense fields",
        ),
        ValidationIssue(
            "acceptance_reveal_mutation",
            "workflow reveal Add expense attempted a mutation",
        ),
        ValidationIssue(
            "acceptance_workflow_mismatch",
            "participant workflow created a recommendation",
        ),
        ValidationIssue(
            "external_url",
            "source contains forbidden capability: external_url",
        ),
        ValidationIssue(
            "acceptance_required_field_missing",
            "participant payload omitted name",
        ),
    )

    await provider.repair(app_blueprint, local, issues, attempt=1)
    repair_prompt = client.responses.requests[-1]["input"][0]["content"]
    assert "ACTIONABLE REPAIR GUIDANCE" in repair_prompt
    assert "one visible WorkflowForm" in repair_prompt
    assert 'operation="records.create"' in repair_prompt
    assert 'exactly one visible Button type="submit"' in repair_prompt
    assert "Make its intent unambiguous" in repair_prompt
    assert "Derived or contextual fields do not need fake inputs" in repair_prompt
    assert "semantic workflow target did not match" in repair_prompt
    assert "do not share a handler or marker" in repair_prompt
    assert "Keep the declared WorkflowForm identity" in repair_prompt
    assert "omit undeclared fields" in repair_prompt
    assert "stores only the value entered by the user" in repair_prompt

    await provider.regenerate(app_blueprint, issues, attempt=2)
    regeneration_prompt = client.responses.requests[-1]["input"][0]["content"]
    assert "same acceptance failure" in regeneration_prompt
    assert "Discard that implementation" in regeneration_prompt
    assert "Do not patch the old workflow again" in regeneration_prompt
    assert "ACCUMULATED DIAGNOSTICS FROM PRIOR CANDIDATES" in regeneration_prompt
    assert "PREVIOUS RESULT" not in regeneration_prompt


def test_source_policy_blocks_privileged_browser_and_dependency_access() -> None:
    source = GeneratedSource(
        files=(
            SourceFile(
                "src/App.tsx",
                'import x from "unknown-package";\nfetch("/private");\nexport default x;',
            ),
        ),
        entrypoint="src/App.tsx",
    )

    codes = {issue.code for issue in inspect_generated_source(source)}
    assert {"dependency_not_allowed", "network_access"} <= codes


def test_source_policy_allows_typescript_comments_but_blocks_real_external_urls() -> None:
    commented = GeneratedSource(
        files=(
            SourceFile(
                "src/App.tsx",
                "// Explain the next line.\nexport default function App() { return <p>ok</p>; }",
            ),
        ),
        entrypoint="src/App.tsx",
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
    )

    assert "reserved_primary_action_marker" in {
        issue.code for issue in inspect_generated_source(source)
    }


def test_source_policy_rejects_direct_workflow_markers() -> None:
    source = GeneratedSource(
        files=(
            SourceFile(
                "src/App.tsx",
                '''import { Card } from "@dot/ui";
export default function App() {
  return <Card data-dot-operation="records.create" data-dot-entity="task">bad</Card>;
}''',
            ),
        ),
        entrypoint="src/App.tsx",
    )

    assert "reserved_workflow_marker" in {
        issue.code for issue in inspect_generated_source(source)
    }


def test_acceptance_plan_follows_manifest_dependency_order() -> None:
    app_blueprint = AppBlueprint.from_mapping(
        {
            **blueprint(),
            "manifest": {
                "schema_version": 1,
                "entities": [
                    {
                        "name": "participant",
                        "fields": {"name": {"type": "string", "required": True}},
                    },
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
                ],
            },
        }
    )
    plan = [dict(step) for step in _acceptance_plan(app_blueprint)]

    assert plan[0] == {
        "operation": "records.list",
        "entity": "participant",
        "required": False,
    }
    assert plan[1] == {
        "operation": "records.list",
        "entity": "expense",
        "required": False,
    }
    assert plan[2]["operation"] == "records.create"
    assert plan[2]["entity"] == "participant"
    assert plan[2]["required"] is True
    assert plan[2]["field_hints"] == {"name": "test"}
    assert "selector" not in plan[3]
    assert plan[3]["required"] is True
    assert plan[3]["event_type"] == "submit"
    assert plan[3]["field_hints"] == {"amount": 1, "note": "test"}
    assert plan[3]["required_payload_fields"] == ["amount"]
    assert plan[3]["allowed_payload_fields"] == [
        "amount",
        "created_at",
        "id",
        "note",
        "updated_at",
        "version",
    ]
    assert plan[3]["operation"] == "records.create"
    assert plan[3]["entity"] == "expense"


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
        assert revision.artifact["browser_bundle"]["format"] == "iife"
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
