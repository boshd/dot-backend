from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from benji_api.app_builder.compiler import AppCompilationError, EsbuildAppCompiler
from benji_api.app_builder.pipeline import AppBuildPipeline
from benji_api.app_builder.providers import DeterministicLocalProvider
from benji_api.app_builder.types import (
    AppBlueprint,
    BuildClaim,
    GeneratedSource,
    SourceFile,
    ValidationIssue,
)


def _document() -> MappingProxyType[str, object]:
    return MappingProxyType(
        {
            "schema_version": 1,
            "data": {},
            "root": {"id": "root", "type": "page", "children": []},
        }
    )


def _source(contents: str, *extra: SourceFile) -> GeneratedSource:
    return GeneratedSource(
        files=(SourceFile("src/App.tsx", contents), *extra),
        entrypoint="src/App.tsx",
        render_document=_document(),
    )


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.anyio
async def test_compiler_emits_self_contained_iife_with_dot_sdk() -> None:
    generated = _source(
        """import { useRecords, runAction } from "@dot/app-runtime";
import { AppShell, Button, Card } from "@dot/ui";
import "./app.css";

export default function App() {
  const { records } = useRecords("task");
  return <AppShell title="weekend"><div className="custom-card"><Card>
    <strong>{records.length} things</strong>
    <Button onClick={() => runAction("records.create", {entity: "task", data: {title: "pack"}})}>
      add one
    </Button>
  </Card></div></AppShell>;
}""",
        SourceFile("src/app.css", ".custom-card { transform: rotate(-0.25deg); }"),
    )

    bundle = await EsbuildAppCompiler().compile(generated)

    assert bundle.format == "iife"
    assert bundle.sdk_version == "2"
    assert bundle.javascript.startswith("(()=>{")
    assert "dot.app.request" in bundle.javascript
    assert "records.list" in bundle.javascript
    assert ".custom-card" in bundle.css
    expected = hashlib.sha256(f"{bundle.javascript}\0{bundle.css}".encode()).hexdigest()
    assert bundle.sha256 == expected


@pytest.mark.anyio
async def test_compiler_includes_branded_sdk_without_generated_css() -> None:
    bundle = await EsbuildAppCompiler().compile(
        _source(
            'import { AppShell, Heading } from "@dot/ui"; '
            "export default () => <AppShell accent=\"sage\"><Heading level={1}>"
            "week</Heading></AppShell>;"
        )
    )

    assert "--dot-accent-solid" in bundle.css
    assert "sage" in bundle.javascript


@pytest.mark.anyio
async def test_primary_workflow_trigger_owns_the_private_acceptance_marker() -> None:
    bundle = await EsbuildAppCompiler().compile(
        _source(
            'import { AppShell, PrimaryWorkflowTrigger } from "@dot/ui"; '
            "export default () => <AppShell><PrimaryWorkflowTrigger "
            'onClick={() => undefined}>start</PrimaryWorkflowTrigger></AppShell>;'
        )
    )

    assert "data-dot-primary-action" in bundle.javascript


@pytest.mark.anyio
async def test_primary_workflow_trigger_rejects_submit_type_and_missing_handler() -> None:
    compiler = EsbuildAppCompiler()
    with pytest.raises(AppCompilationError) as invalid_type:
        await compiler.compile(
            _source(
                'import { PrimaryWorkflowTrigger } from "@dot/ui"; '
                'export default () => <PrimaryWorkflowTrigger type="submit" '
                'onClick={() => undefined}>bad</PrimaryWorkflowTrigger>;'
            )
        )
    with pytest.raises(AppCompilationError) as missing_handler:
        await compiler.compile(
            _source(
                'import { PrimaryWorkflowTrigger } from "@dot/ui"; '
                "export default () => <PrimaryWorkflowTrigger>bad</PrimaryWorkflowTrigger>;"
            )
        )

    assert invalid_type.value.issues[0].code == "typescript_type_error"
    assert missing_handler.value.issues[0].code == "typescript_type_error"


@pytest.mark.anyio
async def test_dot_ui_accepts_model_friendly_collection_and_control_apis() -> None:
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import React, { useState } from "react";
import {
  AppShell, Button, Input, List, ListItem, Segment, SegmentedControl,
  Select, Stack, Text, Textarea
} from "@dot/ui";

export default function App() {
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [priority, setPriority] = useState("normal");
  const [filter, setFilter] = useState("open");
  return <AppShell title="Tasks">
    <Stack gap="large">
      <Input label="Name" value={name} onValueChange={setName} />
      <Textarea label="Notes" value={notes} onValueChange={setNotes} />
      <Select
        label="Priority"
        value={priority}
        onValueChange={setPriority}
        options={[{ value: "normal", label: "Normal" }, { value: "high", label: "High" }]}
      />
      <Button size="small">Save</Button>
    </Stack>
    <Select value={priority} onChange={(event) => setPriority(event.target.value)}>
      <option value="normal">Normal</option><option value="high">High</option>
    </Select>
    <SegmentedControl label="Filter" value={filter} onChange={setFilter}>
      <Segment value="open">Open</Segment><Segment value="done">Done</Segment>
    </SegmentedControl>
    <List>
      <ListItem title="Ship it" detail="Today" />
      <ListItem><Text>Celebrate</Text></ListItem>
    </List>
  </AppShell>;
}'''
        )
    )

    assert bundle.sha256
    assert "High" in bundle.javascript


def test_dot_ui_semantic_color_pairs_meet_normal_text_contrast() -> None:
    css = (
        Path(__file__).parents[1]
        / "src/benji_api/app_builder/compiler/sdk/ui.css"
    ).read_text()
    pairs = re.findall(
        r"--dot-accent-solid:\s*(#[0-9a-f]{6});\s*"
        r"--dot-accent-on-solid:\s*(#[0-9a-f]{6})",
        css,
        flags=re.IGNORECASE,
    )

    assert len(pairs) == 5
    assert all(_contrast(background, foreground) >= 4.5 for background, foreground in pairs)
    assert _contrast("#151512", "#f6f6f2") >= 4.5
    assert _contrast("#696963", "#f6f6f2") >= 4.5
    assert "--dot-chart-muted: #696963" in css
    assert ".dot-input::placeholder { color: var(--dot-muted); opacity: 1; }" in css


@pytest.mark.anyio
async def test_dot_ui_types_reject_visual_escape_hatches() -> None:
    with pytest.raises(AppCompilationError) as invalid:
        await EsbuildAppCompiler().compile(
            _source(
                'import { AppShell, Card } from "@dot/ui"; '
                'export default () => <AppShell><Card style={{ color: "red" }}>bad</Card>'
                "</AppShell>;"
            )
        )

    assert invalid.value.issues[0].code == "typescript_type_error"
    assert "style" in invalid.value.issues[0].message


@pytest.mark.anyio
async def test_compiler_rejects_unapproved_and_unresolved_imports() -> None:
    compiler = EsbuildAppCompiler()
    with pytest.raises(AppCompilationError) as unapproved:
        await compiler.compile(
            _source('import axios from "axios"; export default () => <p>{String(axios)}</p>;')
        )
    assert unapproved.value.issues[0].code == "dependency_not_allowed"

    with pytest.raises(AppCompilationError) as unresolved:
        await compiler.compile(
            _source('import value from "./missing"; export default () => <p>{value}</p>;')
        )
    assert unresolved.value.issues[0].code == "unresolved_import"


@pytest.mark.anyio
async def test_compiler_transpiles_without_executing_generated_module() -> None:
    bundle = await EsbuildAppCompiler().compile(
        _source('throw new Error("must not run at build time"); export default () => <p>safe</p>;')
    )
    assert bundle.sha256


@pytest.mark.anyio
@pytest.mark.parametrize(
    "css",
    [
        '.preview { background-image: image-set("https://evil.test/pixel" 1x); }',
        r'.preview { background-image: image-\73 et("https://evil.test/pixel" 1x); }',
        r".preview { width: ex\70 ression(alert(1)); }",
    ],
)
async def test_compiler_rejects_network_capable_css_after_bundling(css: str) -> None:
    with pytest.raises(AppCompilationError) as invalid:
        await EsbuildAppCompiler().compile(
            _source(
                'import "./app.css"; export default () => <p className="preview">safe</p>;',
                SourceFile("src/app.css", css),
            )
        )

    assert invalid.value.issues[0].code == "compiled_css_network_access"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "css",
    [
        '.preview { background-image: url("https://evil.test/pixel"); }',
        '.preview { background-image: u\\72l("https://evil.test/pixel"); }',
        '@import "https://evil.test/theme.css";',
    ],
)
async def test_compiler_rejects_css_url_and_import_loading(css: str) -> None:
    with pytest.raises(AppCompilationError) as invalid:
        await EsbuildAppCompiler().compile(
            _source(
                'import "./app.css"; export default () => <p className="preview">safe</p>;',
                SourceFile("src/app.css", css),
            )
        )

    assert invalid.value.issues[0].code in {
        "compiled_css_network_access",
        "dependency_not_allowed",
    }


@pytest.mark.anyio
async def test_compiler_runs_real_typescript_typecheck() -> None:
    with pytest.raises(AppCompilationError) as invalid:
        await EsbuildAppCompiler().compile(
            _source('const count: number = "nope"; export default () => <p>{count}</p>;')
        )

    assert invalid.value.issues[0].code == "typescript_type_error"
    assert "string" in invalid.value.issues[0].message


class _CompileRepairProvider(DeterministicLocalProvider):
    def __init__(self) -> None:
        self.repairs = 0
        self.compile_issues: tuple[ValidationIssue, ...] = ()

    async def generate(self, blueprint: AppBlueprint) -> GeneratedSource:
        safe = await super().generate(blueprint)
        return replace(
            safe,
            files=(
                SourceFile(
                    "src/App.tsx",
                    'import value from "./missing"; export default () => <p>{value}</p>;',
                ),
            ),
        )

    async def repair(
        self,
        blueprint: AppBlueprint,
        previous: GeneratedSource,
        issues: tuple[ValidationIssue, ...],
        *,
        attempt: int,
    ) -> GeneratedSource:
        del previous, attempt
        self.repairs += 1
        self.compile_issues = issues
        return await DeterministicLocalProvider().generate(blueprint)


@pytest.mark.anyio
async def test_compile_failure_enters_bounded_model_repair() -> None:
    provider = _CompileRepairProvider()
    completion = await AppBuildPipeline(provider, max_repair_attempts=1).build(
        BuildClaim(
            job_id="job",
            app_id="app",
            revision_id=None,
            blueprint={
                "title": "packing",
                "description": "a packing list",
                "purpose": "remember every item",
            },
        )
    )

    assert provider.repairs == 1
    assert provider.compile_issues[0].code == "unresolved_import"
    assert completion.metrics.repair_attempts == 1
    assert completion.artifact.browser_bundle.sha256
    assert completion.artifact.as_dict()["browser_bundle"]["format"] == "iife"
