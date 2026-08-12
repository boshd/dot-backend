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
import { AppShell, Button, Input, PrimaryWorkflowTrigger } from "@dot/ui";

export default function App() {
  const { records } = useRecords<{ id: string; choice: string }>("vote", { limit: 200 });
  const [open, setOpen] = useState(false);
  const [choice, setChoice] = useState("");
  const [createdChoice, setCreatedChoice] = useState("");
  return <AppShell title="Vote">
    <PrimaryWorkflowTrigger onClick={() => setOpen(true)}>vote</PrimaryWorkflowTrigger>
    {open ? <form data-dot-operation="records.create" data-dot-entity="vote"
      onSubmit={async (event) => {
        event.preventDefault();
        const created = await runAction<{ choice: string }>(
          "records.create", { entity: "vote", data: { choice } }
        );
        setCreatedChoice(created.choice);
      }}>
      <Input label="Choice" name="choice" value={choice}
        onChange={(event) => setChoice(event.currentTarget.value)} />
      <Button type="submit">submit vote</Button>
    </form> : null}
    <p>created: {createdChoice}</p>
    <ul>{records.map((record) => <li key={record.id}>{record.choice}</li>)}</ul>
  </AppShell>;
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
import { AppShell, Button, Input } from "@dot/ui";
export default function App() {
  const [choice, setChoice] = useState("");
  const Form = () => <form data-dot-operation="records.create" data-dot-entity="vote"
    onSubmit={(event) => {
      event.preventDefault();
      void runAction("records.create", { entity: "vote", data: { choice } });
    }}>
    <Input label="Choice" name="choice" value={choice}
      onChange={(event) => setChoice(event.currentTarget.value)} />
    <Button type="submit">submit vote</Button>
  </form>;
  return <AppShell title="Vote"><Form /></AppShell>;
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
import { AppShell, Button, Input } from "@dot/ui";
export default function App() {
  const { records } = useRecords<{ id: string; choice: string }>("vote", { limit: 200 });
  const [choice, setChoice] = useState("");
  return <AppShell title="Vote"><form data-dot-operation="records.create" data-dot-entity="vote"
    onSubmit={(event) => {
      event.preventDefault();
      void runAction("records.create", { entity: "vote", data: { choice } });
      void runAction("records.create", { entity: "vote", data: { choice } });
    }}>
    <Input label="Choice" name="choice" value={choice}
      onChange={(event) => setChoice(event.currentTarget.value)} />
    <Button type="submit">submit vote</Button>
  </form><p>{records.length} votes</p></AppShell>;
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
import { AppShell, Button, Input } from "@dot/ui";
export default function App() {
  const { records } = useRecords<{ id: string; duration: number }>("session");
  const [minutes, setMinutes] = useState("");
  return <AppShell title="Sessions">
    <form onSubmit={(event) => event.preventDefault()}>
      <Button type="submit">unrelated action</Button>
    </form>
    <form data-dot-operation="records.create" data-dot-entity="session"
      onSubmit={(event) => {
        event.preventDefault();
        void runAction("records.create", {
          entity: "session",
          data: { duration: Number(minutes) * 60 },
        });
      }}>
      <Input label="Duration" name="duration" value={minutes}
        onChange={(event) => setMinutes(event.currentTarget.value)} />
      <Button type="submit">save session</Button>
    </form>
    <p>{records.map((record) => record.duration).join(",")} seconds</p>
  </AppShell>;
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
import { AppShell, Button, Select } from "@dot/ui";
export default function App() {
  const { records } = useRecords<{ id: string; priority: string }>("task");
  const [priority, setPriority] = useState("");
  return <AppShell title="Tasks">
    <form data-dot-operation="records.create" data-dot-entity="task"
      onSubmit={(event) => {
        event.preventDefault();
        void runAction("records.create", { entity: "task", data: { priority } });
      }}>
      <Select label="Priority" name="priority" value={priority}
        onChange={(event) => setPriority(event.currentTarget.value)}>
        <option value="" disabled>pick one</option>
        <option value="low">low</option>
        <option value="high">high</option>
      </Select>
      <Button type="submit">save task</Button>
    </form>
    <p>{records.map((record) => record.priority).join(",")}</p>
  </AppShell>;
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
import { AppShell, Button } from "@dot/ui";
export default function App() {
  const { records } = useRecords<{ id: string; choice: string }>("vote");
  return <AppShell title="Vote">
    <form data-dot-operation="records.create" data-dot-entity="vote"
      onSubmit={(event) => {
        event.preventDefault();
        void runAction("records.create", { entity: "vote", data: { choice: "yes" } });
      }}>
      <p>your current choice is yes</p>
      <Button type="submit">vote yes</Button>
    </form>
    <p>{records.map((record) => record.choice).join(",")}</p>
  </AppShell>;
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


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("expected_code", "source"),
    [
        (
            "ux_giant_heading",
            '''export default function App() {
  return <main><h1 style={{ fontSize: 80 }}>Cottage weekend expense planner</h1></main>;
}''',
        ),
        (
            "ux_horizontal_overflow",
            '''export default function App() {
  return <main><h1>Trip planner</h1><div style={{ width: 600 }}>Trip details</div></main>;
}''',
        ),
        (
            "ux_primary_control_clipped",
            '''export default function App() {
  return <main><h1>Trip planner</h1><button data-dot-primary-action
    style={{ position: "fixed", left: 370, top: 100, width: 44, height: 44 }}>
    Add
  </button></main>;
}''',
        ),
        (
            "ux_tap_target_too_small",
            '''export default function App() {
  return <main><h1>Trip planner</h1><button style={{ width: 43, height: 43 }}>Add</button></main>;
}''',
        ),
        (
            "ux_missing_control_label",
            '''export default function App() {
  return <main><h1>Trip planner</h1><input style={{ width: 180, height: 44 }} /></main>;
}''',
        ),
        (
            "ux_raw_json_copy",
            '''export default function App() {
  return <main><h1>Trip planner</h1><p>Paste valid JSON to configure the trip.</p></main>;
}''',
        ),
        (
            "ux_schema_admin_copy",
            '''export default function App() {
  return <main><h1>Trip planner</h1><p>Configure the expense entity schema.</p></main>;
}''',
        ),
        (
            "ux_visible_identifier",
            '''export default function App() {
  return <main><h1>Trip planner</h1><p>Enter the guest_name below.</p></main>;
}''',
        ),
    ],
)
async def test_chromium_acceptance_rejects_mobile_ux_regressions(
    expected_code: str,
    source: str,
) -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(_source(source))

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle)

    assert failed.value.issues[0].code == expected_code


@pytest.mark.anyio
async def test_chromium_ux_audit_only_checks_user_visible_copy() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''export default function App() {
  const internal_field_name = "guest_name";
  return <main data-schema-version="trip_expense">
    <h1>Trip planner</h1>
    <label>Guest name
      <input name={internal_field_name} style={{ width: 180, height: 44 }} />
    </label>
    <button data-dot-operation="records.create" data-dot-entity="trip_expense"
      style={{ width: 120, height: 44 }}>Add guest</button>
  </main>;
}'''
        )
    )

    result = await runner.smoke(bundle)

    assert result["ux_audit"] == {
        "passed": True,
        "viewport": {"width": 390, "height": 844},
    }


@pytest.mark.anyio
async def test_chromium_ux_audit_accepts_all_compact_dot_controls() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import {
  AppShell, Button, Checkbox, Segment, SegmentedControl
} from "@dot/ui";
export default function App() {
  const [filter, setFilter] = useState("open");
  const [done, setDone] = useState(false);
  return <AppShell title="Trip planner">
    <Button size="sm">Add guest</Button>
    <SegmentedControl label="Filter" value={filter} onValueChange={setFilter}>
      <Segment value="open">Open</Segment>
      <Segment value="done">Done</Segment>
    </SegmentedControl>
    <Checkbox label="Packed" checked={done} onCheckedChange={setDone} />
  </AppShell>;
}'''
        )
    )

    result = await runner.smoke(bundle)

    assert result["ux_audit"]["passed"] is True
