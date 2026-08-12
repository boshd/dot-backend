from __future__ import annotations

import pytest

from benji_api.app_builder.compiler import EsbuildAppCompiler
from benji_api.app_builder.compiler.source_normalizer import normalize_generated_source
from benji_api.app_builder.policy import inspect_generated_source
from benji_api.app_builder.types import GeneratedSource, SourceFile


@pytest.mark.anyio
async def test_typescript_normalizer_removes_only_jsx_visual_escape_hatches() -> None:
    contents = '''import React, { useState } from "react";
import { AppShell, Card, Input } from "@dot/ui";
const domain = { style: "compact", className: "expense-category" };
export default function App(): JSX.Element {
  const [name, setName] = useState("");
  return <AppShell title="trip" style={{ color: "red" }}>
    <Card className="wide">{domain.style}<style>{`.wide { color: red; }`}</style></Card>
    <Input value={name} onChange={setName} />
  </AppShell>;
}'''

    files, counts = await normalize_generated_source(
        [SourceFile("src/App.tsx", contents)]
    )

    normalized = files[0].contents
    assert 'style: "compact"' in normalized
    assert 'className: "expense-category"' in normalized
    assert "style={{" not in normalized
    assert 'className="wide"' not in normalized
    assert "<style>" not in normalized
    assert "onValueChange={setName}" in normalized
    assert "React.ReactElement" in normalized
    assert counts == {
        "normalized_react_types": 1,
        "normalized_value_handlers": 1,
        "stripped_class_names": 1,
        "stripped_inline_styles": 1,
        "stripped_style_elements": 1,
    }
    generated = GeneratedSource(
        files=tuple(files),
        entrypoint="src/App.tsx",
    )
    assert not inspect_generated_source(generated)
    assert (await EsbuildAppCompiler().compile(generated)).sha256


def test_policy_does_not_confuse_domain_style_keys_with_jsx_styling() -> None:
    source = GeneratedSource(
        files=(
            SourceFile(
                "src/App.tsx",
                '''const preferences = { style: "compact", className: "expense-category" };
export default function App() { return <p>{preferences.style}</p>; }''',
            ),
        ),
        entrypoint="src/App.tsx",
    )

    codes = {issue.code for issue in inspect_generated_source(source)}

    assert "inline_style" not in codes
    assert "custom_class_name" not in codes


def test_policy_reports_source_coordinates_for_repairs() -> None:
    source = GeneratedSource(
        files=(
            SourceFile(
                "src/App.tsx",
                'const ok = true;\nfetch("/private");\nexport default () => <p>{ok}</p>;',
            ),
        ),
        entrypoint="src/App.tsx",
    )

    issue = next(
        item for item in inspect_generated_source(source) if item.code == "network_access"
    )

    assert issue.path == "src/App.tsx:2:1"
