from __future__ import annotations

from types import MappingProxyType

import pytest

from benji_api.app_builder.browser_smoke import (
    AppBrowserSmokeError,
    ChromiumAppAcceptanceRunner,
    QuickJSAppSmokeRunner,
)
from benji_api.app_builder.compiler import EsbuildAppCompiler
from benji_api.app_builder.types import BrowserBundle, GeneratedSource, SourceFile


def _source(contents: str) -> GeneratedSource:
    return GeneratedSource(
        files=(SourceFile("src/App.tsx", contents),),
        entrypoint="src/App.tsx",
        render_document=MappingProxyType(
            {
                "schema_version": 1,
                "data": {},
                "root": {"id": "root", "type": "page", "children": []},
            }
        ),
    )


def test_chromium_runner_requires_the_native_sandbox_by_default() -> None:
    assert ChromiumAppAcceptanceRunner().sandbox_required is True


@pytest.mark.anyio
async def test_quickjs_guest_renders_a_trivial_app_and_reports_ready() -> None:
    runner = QuickJSAppSmokeRunner(timeout_seconds=10)
    if not runner.available:
        pytest.skip("QuickJS smoke runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source("export default function App() { return <p>hello from dot</p>; }")
    )

    result = await runner.smoke(bundle)

    assert result["ready"] is True
    assert result["rendered"] is True
    assert result["runtime"] == "quickjs-wasm"
    assert result["runtime_errors"] == 0
    assert result["static_html"] == "<p>hello from dot</p>"


@pytest.mark.anyio
async def test_quickjs_guest_renders_and_reads_without_exercising_actions() -> None:
    runner = QuickJSAppSmokeRunner(timeout_seconds=10, simulate_first_click=True)
    if not runner.available:
        pytest.skip("QuickJS smoke runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            """import { runAction, useRecords } from "@dot/app-runtime";

export default function App() {
  const { records } = useRecords("task");
  return <><p>{records.length} tasks</p><button onClick={() => void runAction(
    "records.create", { entity: "task", data: { title: "pack" } }
  )}>add</button></>;
}"""
        )
    )

    result = await runner.smoke(bundle)

    assert result["ready"] is True
    assert result["runtime_errors"] == 0
    assert result["record_read_exercised"] is True
    assert result["record_write_exercised"] is False
    assert result["record_writes"] == 0
    assert "0 tasks" in result["static_html"]


@pytest.mark.anyio
async def test_quickjs_guest_ignores_behavioral_acceptance_plans() -> None:
    runner = QuickJSAppSmokeRunner(timeout_seconds=10)
    if not runner.available:
        pytest.skip("QuickJS smoke runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source("export default function App() { return <p>rendered safely</p>; }")
    )

    result = await runner.smoke(
        bundle,
        acceptance_plan=(
            {
                "operation": "records.create",
                "entity": "task",
                "required": True,
                "selector": '[data-dot-operation="records.create"][data-dot-entity="task"]',
                "event_type": "submit",
            },
        ),
    )

    assert result["ready"] is True
    assert result["record_writes"] == 0
    assert "acceptance" not in result


@pytest.mark.anyio
async def test_browser_rejects_background_record_mutation() -> None:
    runner = QuickJSAppSmokeRunner(timeout_seconds=10)
    if not runner.available:
        pytest.skip("QuickJS smoke runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            """import { useEffect } from "react";
import { runAction } from "@dot/app-runtime";
export default function App() {
  useEffect(() => { void runAction("records.create", { entity: "task", data: {} }); }, []);
  return <p>hello</p>;
}"""
        )
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle)

    assert any(issue.code == "background_mutation" for issue in failed.value.issues)


@pytest.mark.anyio
async def test_browser_rejects_runtime_exception_before_promotion() -> None:
    runner = QuickJSAppSmokeRunner(timeout_seconds=10)
    if not runner.available:
        pytest.skip("QuickJS smoke runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source('throw new Error("boom at runtime"); export default () => <p>never</p>;')
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle)

    assert any(issue.code == "browser_runtime_error" for issue in failed.value.issues)


@pytest.mark.anyio
async def test_quickjs_guest_interrupts_runaway_generated_code() -> None:
    runner = QuickJSAppSmokeRunner(timeout_seconds=1)
    if not runner.available:
        pytest.skip("QuickJS smoke runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source("while (true) {} export default () => <p>never</p>;")
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle)

    assert any(issue.code == "browser_smoke_timeout" for issue in failed.value.issues)


@pytest.mark.anyio
async def test_generated_code_cannot_forge_smoke_controls_or_user_gesture() -> None:
    runner = QuickJSAppSmokeRunner(timeout_seconds=10)
    if not runner.available:
        pytest.skip("QuickJS smoke runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            """import { runAction } from "@dot/app-runtime";
const host = globalThis as typeof globalThis & {
  __dotSmokeClick?: () => void;
  __dotSmokeResult?: () => string;
};
host.__dotSmokeClick?.();
host.__dotSmokeResult = () => JSON.stringify({ ok: true, result: { ready: true } });
void runAction("records.create", { entity: "task", data: {} });
export default () => <p>trying to cheat</p>;"""
        )
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle)

    assert any(issue.code == "background_mutation" for issue in failed.value.issues)


@pytest.mark.anyio
async def test_generated_code_cannot_forge_smoke_result_by_poisoning_primordials() -> None:
    runner = QuickJSAppSmokeRunner(timeout_seconds=10)
    if not runner.available:
        pytest.skip("QuickJS smoke runtime is unavailable")
    forged = BrowserBundle(
        format="iife",
        javascript="""
globalThis.JSON = {
  parse: () => ({}),
  stringify: () =>
    '{"ok":true,"issues":[],"result":{"ready":true,"rendered":true,' +
    '"static_html":"<p>forged</p>"}}'
};
try { Set.prototype.has = () => true; } catch {}
""",
        css="",
        sha256="0" * 64,
        sdk_version="1",
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(forged)

    assert any(issue.code == "browser_not_ready" for issue in failed.value.issues)


@pytest.mark.anyio
async def test_quickjs_guest_exposes_no_host_or_network_apis() -> None:
    runner = QuickJSAppSmokeRunner(timeout_seconds=10)
    if not runner.available:
        pytest.skip("QuickJS smoke runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            """const scope = globalThis as Record<string, unknown>;
const forbidden = [
  typeof scope.process, typeof scope.require, typeof scope.fetch, typeof scope.XMLHttpRequest,
  typeof scope.WebSocket, typeof scope.Worker, typeof scope.WebAssembly,
];
if (forbidden.some((value) => value !== "undefined")) {
  throw new Error(`host api leaked: ${forbidden}`);
}
export default () => <p>isolated</p>;
"""
        )
    )

    result = await runner.smoke(bundle)

    assert result["ready"] is True
    assert result["runtime"] == "quickjs-wasm"


@pytest.mark.anyio
async def test_quickjs_guest_rejects_oversized_static_render_without_echoing_it() -> None:
    runner = QuickJSAppSmokeRunner(timeout_seconds=10)
    if not runner.available:
        pytest.skip("QuickJS smoke runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source("export default () => <p>{'x'.repeat(256_001)}</p>;")
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle)

    assert any(issue.code == "static_render_too_large" for issue in failed.value.issues)
