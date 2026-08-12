from __future__ import annotations

from types import MappingProxyType

import pytest

from benji_api.app_builder.browser_smoke import (
    AppBrowserSmokeError,
    ChromiumAppAcceptanceRunner,
)
from benji_api.app_builder.compiler import EsbuildAppCompiler
from benji_api.app_builder.types import GeneratedSource, SourceFile


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


def _vote_plan() -> tuple[dict[str, object], ...]:
    return (
        {
            "operation": "ui.reveal_primary",
            "required": False,
            "selector": "[data-dot-primary-action]",
            "event_type": "click",
        },
        {
            "operation": "records.create",
            "entity": "vote",
            "required": True,
            "selector": '[data-dot-operation="records.create"][data-dot-entity="vote"]',
            "event_type": "submit",
            "field_hints": {"choice": "test"},
            "required_payload_fields": ["choice"],
            "allowed_payload_fields": ["choice"],
        },
    )


@pytest.mark.anyio
async def test_chromium_acceptance_types_without_losing_focus_and_renders_saved_record() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction, useRecords } from "@dot/app-runtime";
import { PrimaryWorkflowTrigger } from "@dot/ui";

export default function App() {
  const { records } = useRecords<{ id: string; choice: string }>("vote", { limit: 200 });
  const [open, setOpen] = useState(false);
  const [choice, setChoice] = useState("");
  const [createdChoice, setCreatedChoice] = useState("");
  return <main>
    <PrimaryWorkflowTrigger onClick={() => setOpen(true)}>vote</PrimaryWorkflowTrigger>
    {open ? <form data-dot-operation="records.create" data-dot-entity="vote"
      onSubmit={async (event) => {
        event.preventDefault();
        const created = await runAction<{ choice: string }>(
          "records.create", { entity: "vote", data: { choice } }
        );
        setCreatedChoice(created.choice);
      }}>
      <input name="choice" value={choice}
        onChange={(event) => setChoice(event.currentTarget.value)} />
      <button type="submit">submit vote</button>
    </form> : null}
    <p>created: {createdChoice}</p>
    <ul>{records.map((record) => <li key={record.id}>{record.choice}</li>)}</ul>
  </main>;
}'''
        )
    )

    result = await runner.smoke(bundle, acceptance_plan=_vote_plan())

    assert result["runtime"] == "chromium"
    assert result["process_sandbox"] == "disabled-explicit-railway-fallback"
    assert result["required_mutations_verified"] == 1
    assert result["record_refreshes_verified"] == 1
    assert result["persisted_renders_verified"] == 1
    assert result["field_typing"] == [
        {"field": "choice", "mode": "trusted-keyboard", "value": "test", "characters": 4}
    ]
    mutations = [operation for operation in result["operations"] if operation["mutating"]]
    assert len(mutations) == 1
    assert mutations[0]["args"]["data"] == {"choice": "test"}
    record_reads = [
        operation
        for operation in result["operations"]
        if operation["operation"] == "records.list"
    ]
    assert len(record_reads) >= 2
    assert all(operation["args"]["limit"] == 100 for operation in record_reads)


@pytest.mark.anyio
async def test_chromium_acceptance_rejects_input_remounted_after_each_character() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction } from "@dot/app-runtime";
export default function App() {
  const [choice, setChoice] = useState("");
  const Form = () => <form data-dot-operation="records.create" data-dot-entity="vote"
    onSubmit={(event) => {
      event.preventDefault();
      void runAction("records.create", { entity: "vote", data: { choice } });
    }}>
    <input name="choice" value={choice}
      onChange={(event) => setChoice(event.currentTarget.value)} />
    <button type="submit">submit vote</button>
  </form>;
  return <Form />;
}'''
        )
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle, acceptance_plan=(_vote_plan()[1],))

    assert failed.value.issues[0].code == "acceptance_input_focus_lost"


@pytest.mark.anyio
async def test_chromium_acceptance_rejects_duplicate_submit_mutations() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction, useRecords } from "@dot/app-runtime";
export default function App() {
  const { records } = useRecords<{ id: string; choice: string }>("vote", { limit: 200 });
  const [choice, setChoice] = useState("");
  return <><form data-dot-operation="records.create" data-dot-entity="vote"
    onSubmit={(event) => {
      event.preventDefault();
      void runAction("records.create", { entity: "vote", data: { choice } });
      void runAction("records.create", { entity: "vote", data: { choice } });
    }}>
    <input name="choice" value={choice}
      onChange={(event) => setChoice(event.currentTarget.value)} />
    <button type="submit">submit vote</button>
  </form><p>{records.length} votes</p></>;
}'''
        )
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle, acceptance_plan=(_vote_plan()[1],))

    assert failed.value.issues[0].code in {
        "acceptance_duplicate_mutation",
        "background_mutation",
    }


@pytest.mark.anyio
async def test_chromium_acceptance_allows_valid_storage_transformation_and_scopes_submit() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction, useRecords } from "@dot/app-runtime";
export default function App() {
  const { records } = useRecords<{ id: string; duration: number }>("session");
  const [minutes, setMinutes] = useState("");
  return <>
    <form onSubmit={(event) => event.preventDefault()}>
      <button type="submit">unrelated action</button>
    </form>
    <form data-dot-operation="records.create" data-dot-entity="session"
      onSubmit={(event) => {
        event.preventDefault();
        void runAction("records.create", {
          entity: "session",
          data: { duration: Number(minutes) * 60 },
        });
      }}>
      <input name="duration" value={minutes}
        onChange={(event) => setMinutes(event.currentTarget.value)} />
      <button type="submit">save session</button>
    </form>
    <p>{records.map((record) => record.duration).join(",")} seconds</p>
  </>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "session",
            "required": True,
            "selector": (
                '[data-dot-operation="records.create"][data-dot-entity="session"]'
            ),
            "event_type": "submit",
            "fields": {"duration": 1},
            "required_fields": ["duration"],
            "allowed_fields": ["duration"],
        },
    )

    result = await runner.smoke(bundle, acceptance_plan=plan)

    mutation = next(item for item in result["operations"] if item["mutating"])
    assert mutation["args"]["data"] == {"duration": 60}
    assert result["persisted_renders_verified"] == 1


@pytest.mark.anyio
async def test_chromium_acceptance_selects_a_real_enabled_option() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction, useRecords } from "@dot/app-runtime";
export default function App() {
  const { records } = useRecords<{ id: string; priority: string }>("task");
  const [priority, setPriority] = useState("");
  return <>
    <form data-dot-operation="records.create" data-dot-entity="task"
      onSubmit={(event) => {
        event.preventDefault();
        void runAction("records.create", { entity: "task", data: { priority } });
      }}>
      <select name="priority" value={priority}
        onChange={(event) => setPriority(event.currentTarget.value)}>
        <option value="" disabled>pick one</option>
        <option value="low">low</option>
        <option value="high">high</option>
      </select>
      <button type="submit">save task</button>
    </form>
    <p>{records.map((record) => record.priority).join(",")}</p>
  </>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "task",
            "required": True,
            "selector": '[data-dot-operation="records.create"][data-dot-entity="task"]',
            "event_type": "submit",
            # This generic suggestion is deliberately not a real option.
            "field_hints": {"priority": "test"},
            "required_payload_fields": ["priority"],
            "allowed_payload_fields": ["priority"],
        },
    )

    result = await runner.smoke(bundle, acceptance_plan=plan)

    mutation = next(item for item in result["operations"] if item["mutating"])
    assert mutation["args"]["data"] == {"priority": "low"}
    assert result["field_typing"] == [
        {"field": "priority", "mode": "select", "value": "low"}
    ]


@pytest.mark.anyio
async def test_chromium_acceptance_validates_payload_without_requiring_named_input() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { runAction, useRecords } from "@dot/app-runtime";
export default function App() {
  const { records } = useRecords<{ id: string; choice: string }>("vote");
  return <>
    <form data-dot-operation="records.create" data-dot-entity="vote"
      onSubmit={(event) => {
        event.preventDefault();
        void runAction("records.create", { entity: "vote", data: { choice: "yes" } });
      }}>
      <p>your current choice is yes</p>
      <button type="submit">vote yes</button>
    </form>
    <p>{records.map((record) => record.choice).join(",")}</p>
  </>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "vote",
            "required": True,
            "selector": '[data-dot-operation="records.create"][data-dot-entity="vote"]',
            "event_type": "submit",
            "field_hints": {"choice": "test"},
            "required_payload_fields": ["choice"],
            "allowed_payload_fields": ["choice"],
        },
    )

    result = await runner.smoke(bundle, acceptance_plan=plan)

    mutation = next(item for item in result["operations"] if item["mutating"])
    assert mutation["args"]["data"] == {"choice": "yes"}
    assert result["field_typing"] == []
