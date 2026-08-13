from __future__ import annotations

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
    )


def _vote_plan() -> tuple[dict[str, object], ...]:
    return (
        {
            "operation": "records.create",
            "entity": "vote",
            "required": True,
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
    {open ? <form
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
  const Form = () => <form
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
        await runner.smoke(bundle, acceptance_plan=_vote_plan())

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
  return <AppShell title="Vote"><form
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
        await runner.smoke(bundle, acceptance_plan=_vote_plan())

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
    <form
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
    <form
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
async def test_chromium_acceptance_handles_repeated_named_checkboxes_as_an_array() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction, useRecords } from "@dot/app-runtime";
import { AppShell, Button, Checkbox } from "@dot/ui";
export default function App() {
  const { records } = useRecords<{ id: string; split_between: string[] }>("expense");
  const [people, setPeople] = useState<string[]>([]);
  const toggle = (name: string, checked: boolean) => setPeople((current) =>
    checked ? [...current, name] : current.filter((item) => item !== name));
  return <AppShell title="Trip expenses"><form onSubmit={(event) => {
    event.preventDefault();
    void runAction("records.create", {
      entity: "expense", data: { split_between: people },
    });
  }}>
    <Checkbox label="Kareem" name="split_between" value="Kareem"
      checked={people.includes("Kareem")}
      onCheckedChange={(checked) => toggle("Kareem", checked)} />
    <Checkbox label="Maged" name="split_between" value="Maged"
      checked={people.includes("Maged")}
      onCheckedChange={(checked) => toggle("Maged", checked)} />
    <Button type="submit">add expense</Button>
  </form><p>{records.map((record) => record.split_between.join(", ")).join("; ")}</p></AppShell>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "expense",
            "required": True,
            "event_type": "submit",
            "field_hints": {"split_between": []},
            "required_payload_fields": ["split_between"],
            "allowed_payload_fields": ["split_between"],
        },
    )

    result = await runner.smoke(bundle, acceptance_plan=plan)

    mutation = next(item for item in result["operations"] if item["mutating"])
    assert mutation["args"]["data"] == {"split_between": ["Kareem"]}
    assert result["field_typing"] == [
        {"field": "split_between", "mode": "multi-choice", "value": ["Kareem"]}
    ]


@pytest.mark.anyio
async def test_chromium_acceptance_allows_dynamic_array_builder_without_array_control() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction, useRecords } from "@dot/app-runtime";
import { AppShell, Button, Input } from "@dot/ui";
export default function App() {
  const { records } = useRecords<{ id: string; name: string; exercise_names: string[] }>("routine");
  const [name, setName] = useState("");
  const [exercise, setExercise] = useState("Bench press");
  return <AppShell title="Routines"><form onSubmit={(event) => {
    event.preventDefault();
    void runAction("records.create", {
      entity: "routine", data: { name, exercise_names: [exercise] },
    });
  }}>
    <Input label="Routine name" name="name" value={name} onValueChange={setName} />
    <Input label="First exercise" name="exercise_draft" value={exercise}
      onValueChange={setExercise} />
    <Button type="submit">save routine</Button>
  </form>
  <p>{records.map((record) => `${record.name}: ${record.exercise_names.join(", ")}`).join("; ")}</p>
  </AppShell>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "routine",
            "required": True,
            "event_type": "submit",
            "field_hints": {"name": "test", "exercise_names": []},
            "required_payload_fields": ["name", "exercise_names"],
            "allowed_payload_fields": ["name", "exercise_names"],
        },
    )

    result = await runner.smoke(bundle, acceptance_plan=plan)

    mutation = next(item for item in result["operations"] if item["mutating"])
    assert mutation["args"]["data"] == {
        "name": "test",
        "exercise_names": ["Bench press"],
    }
    assert result["field_typing"] == [
        {"field": "name", "mode": "trusted-keyboard", "value": "test", "characters": 4}
    ]


@pytest.mark.anyio
async def test_chromium_acceptance_formats_datetime_local_for_browser_fill() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction, useRecords } from "@dot/app-runtime";
import { AppShell, Button, Input } from "@dot/ui";
export default function App() {
  const { records } = useRecords<{ id: string; started_at: string }>("workout");
  const [startedAt, setStartedAt] = useState("");
  return <AppShell title="Workout"><form onSubmit={(event) => {
    event.preventDefault();
    void runAction("records.create", {
      entity: "workout", data: { started_at: startedAt },
    });
  }}>
    <Input type="datetime-local" label="Started at" name="started_at"
      value={startedAt} onValueChange={setStartedAt} />
    <Button type="submit">start workout</Button>
  </form><p>{records.map((record) => record.started_at).join(", ")}</p></AppShell>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "workout",
            "required": True,
            "event_type": "submit",
            "field_hints": {"started_at": "2026-01-01T09:00:00Z"},
            "required_payload_fields": ["started_at"],
            "allowed_payload_fields": ["started_at"],
        },
    )

    result = await runner.smoke(bundle, acceptance_plan=plan)

    mutation = next(item for item in result["operations"] if item["mutating"])
    assert mutation["args"]["data"] == {"started_at": "2026-01-01T09:00"}
    assert result["field_typing"] == [
        {"field": "started_at", "mode": "browser-fill", "value": "2026-01-01T09:00"}
    ]


@pytest.mark.anyio
async def test_chromium_acceptance_reveals_marker_free_dependency_then_primary_form() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction, useRecords } from "@dot/app-runtime";
import {
  AppShell, Button, Checkbox, Input, PrimaryWorkflowTrigger, Select
} from "@dot/ui";
export default function App() {
  const { records: people } = useRecords<{ id: string; name: string }>("participant");
  const { records: expenses } = useRecords<{ id: string; amount: number }>("expense");
  const [peopleOpen, setPeopleOpen] = useState(false);
  const [name, setName] = useState("");
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [paidBy, setPaidBy] = useState("");
  const [split, setSplit] = useState(false);
  return <AppShell title="Cottage costs">
    <Button aria-label="Add participant" onClick={() => setPeopleOpen(true)}>add tripmate</Button>
    {peopleOpen ? <form onSubmit={(event) => {
      event.preventDefault();
      void runAction("records.create", { entity: "participant", data: { name } });
    }}>
      <Input label="Tripmate" name="name" value={name} onValueChange={setName} />
      <Button type="submit">add tripmate</Button>
    </form> : null}
    <PrimaryWorkflowTrigger onClick={() => setOpen(true)}>add expense</PrimaryWorkflowTrigger>
    {open ? <form onSubmit={(event) => {
      event.preventDefault();
      void runAction("records.create", { entity: "expense", data: {
        amount: Number(amount), paid_by: paidBy, split_between: split ? [paidBy] : [],
      }});
    }}>
      <Input label="Amount" name="amount" value={amount} onValueChange={setAmount} />
      <Select label="Paid by" name="paid_by" value={paidBy} onValueChange={setPaidBy}
        options={people.map((person) => ({ value: person.name, label: person.name }))} />
      <Checkbox label="Split with tripmates" name="split_between" checked={split}
        value={paidBy} onCheckedChange={setSplit} />
      <Button type="submit">save expense</Button>
    </form> : null}
    <p>{people.length} tripmates, {expenses.length} expenses</p>
  </AppShell>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "participant",
            "required": True,
            "event_type": "submit",
            "field_hints": {"name": "test"},
            "required_payload_fields": ["name"],
            "allowed_payload_fields": ["name"],
        },
        {
            "operation": "records.create",
            "entity": "expense",
            "required": False,
            "event_type": "submit",
            "field_hints": {"amount": 1, "paid_by": "test", "split_between": []},
            "required_payload_fields": ["amount", "paid_by", "split_between"],
            "allowed_payload_fields": ["amount", "paid_by", "split_between"],
        },
    )

    result = await runner.smoke(bundle, acceptance_plan=plan)

    mutations = [item for item in result["operations"] if item["mutating"]]
    assert [item["args"]["entity"] for item in mutations] == ["participant", "expense"]
    assert mutations[1]["args"]["data"] == {
        "amount": 1,
        "paid_by": "test",
        "split_between": ["test"],
    }
    assert result["workflow_reveals"] == [
        {"entity": "participant", "label": "Add participant add tripmate", "matched": True},
        {"entity": "expense", "label": "add expense", "matched": True},
    ]


@pytest.mark.anyio
async def test_chromium_acceptance_uses_exact_workflow_marker_when_forms_share_fields() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction, useRecords } from "@dot/app-runtime";
import {
  AppShell, Button, Input, PrimaryWorkflowTrigger, WorkflowForm
} from "@dot/ui";
export default function App() {
  const { records: recommendations } = useRecords<{ id: string; name: string }>("recommendation");
  const { records: participants } = useRecords<{ id: string; name: string }>("participant");
  const [recommendation, setRecommendation] = useState("");
  const [participant, setParticipant] = useState("");
  const [participantOpen, setParticipantOpen] = useState(false);
  return <AppShell title="Portugal trip">
    <WorkflowForm entity="recommendation" onSubmit={(event) => {
      event.preventDefault();
      void runAction("records.create", {
        entity: "recommendation", data: { name: recommendation },
      });
    }}>
      <Input label="Recommendation" name="name" value={recommendation}
        onValueChange={setRecommendation} />
      <Button type="submit">save recommendation</Button>
    </WorkflowForm>
    <PrimaryWorkflowTrigger entity="participant"
      onClick={() => setParticipantOpen(true)}>add participant</PrimaryWorkflowTrigger>
    {participantOpen ? <WorkflowForm entity="participant" onSubmit={(event) => {
      event.preventDefault();
      void runAction("records.create", {
        entity: "participant", data: { name: participant },
      });
    }}>
      <Input label="Participant" name="name" value={participant}
        onValueChange={setParticipant} />
      <Button type="submit">save participant</Button>
    </WorkflowForm> : null}
    <p>{recommendations.length} recommendations</p>
    <p>{participants.map((person) => person.name).join(", ")}</p>
  </AppShell>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "participant",
            "required": True,
            "event_type": "submit",
            "field_hints": {"name": "test"},
            "required_payload_fields": ["name"],
            "allowed_payload_fields": ["name"],
        },
    )

    result = await runner.smoke(bundle, acceptance_plan=plan)

    mutation = next(item for item in result["operations"] if item["mutating"])
    assert mutation["args"] == {"entity": "participant", "data": {"name": "test"}}
    assert result["workflow_reveals"] == [
        {
            "entity": "participant",
            "label": "add participant",
            "matched": True,
            "semantic": True,
        }
    ]


@pytest.mark.anyio
async def test_chromium_acceptance_rejects_semantic_form_with_wrong_mutation() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction } from "@dot/app-runtime";
import { AppShell, Button, Input, WorkflowForm } from "@dot/ui";
export default function App() {
  const [name, setName] = useState("");
  return <AppShell title="Trip">
    <WorkflowForm entity="participant" onSubmit={(event) => {
      event.preventDefault();
      void runAction("records.create", {
        entity: "recommendation", data: { name },
      });
    }}>
      <Input label="Participant" name="name" value={name} onValueChange={setName} />
      <Button type="submit">save participant</Button>
    </WorkflowForm>
  </AppShell>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "participant",
            "required": True,
            "event_type": "submit",
            "field_hints": {"name": "test"},
            "required_payload_fields": ["name"],
            "allowed_payload_fields": ["name"],
        },
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle, acceptance_plan=plan)

    assert failed.value.issues[0].code == "acceptance_workflow_mismatch"
    assert "expected operation=records.create entity=participant" in failed.value.issues[0].message
    assert "observed operation=records.create entity=recommendation" in (
        failed.value.issues[0].message
    )


@pytest.mark.anyio
async def test_chromium_acceptance_rejects_invalid_payload_from_exact_workflow() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction } from "@dot/app-runtime";
import { AppShell, Button, Input, WorkflowForm } from "@dot/ui";
export default function App() {
  const [name, setName] = useState("");
  return <AppShell title="Trip">
    <WorkflowForm entity="participant" onSubmit={(event) => {
      event.preventDefault();
      void runAction("records.create", {
        entity: "participant", data: { nickname: name },
      });
    }}>
      <Input label="Participant" name="name" value={name} onValueChange={setName} />
      <Button type="submit">save participant</Button>
    </WorkflowForm>
  </AppShell>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "participant",
            "required": True,
            "event_type": "submit",
            "field_hints": {"name": "test"},
            "required_payload_fields": ["name"],
            "allowed_payload_fields": ["name"],
        },
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle, acceptance_plan=plan)

    assert failed.value.issues[0].code == "acceptance_required_field_missing"
    assert 'required=["name"]' in failed.value.issues[0].message
    assert 'allowed=["name"]' in failed.value.issues[0].message
    assert 'observed_keys=["nickname"]' in failed.value.issues[0].message


@pytest.mark.anyio
async def test_chromium_acceptance_never_falls_back_when_exact_hidden_workflow_exists() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction } from "@dot/app-runtime";
import { AppShell, Button, Input, WorkflowForm } from "@dot/ui";
export default function App() {
  const [recommendation, setRecommendation] = useState("");
  const [participant, setParticipant] = useState("");
  return <AppShell title="Portugal trip">
    <form onSubmit={(event) => {
      event.preventDefault();
      void runAction("records.create", {
        entity: "recommendation", data: { name: recommendation },
      });
    }}>
      <Input label="Recommendation" name="name" value={recommendation}
        onValueChange={setRecommendation} />
      <Button type="submit">save recommendation</Button>
    </form>
    <div hidden>
      <WorkflowForm entity="participant" onSubmit={(event) => {
        event.preventDefault();
        void runAction("records.create", {
          entity: "participant", data: { name: participant },
        });
      }}>
        <Input label="Participant" name="name" value={participant}
          onValueChange={setParticipant} />
        <Button type="submit">save participant</Button>
      </WorkflowForm>
    </div>
  </AppShell>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "participant",
            "required": True,
            "event_type": "submit",
            "field_hints": {"name": "test"},
            "required_payload_fields": ["name"],
            "allowed_payload_fields": ["name"],
        },
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle, acceptance_plan=plan)

    assert failed.value.issues[0].code == "acceptance_flow_missing"
    assert 'expected={"operation":"records.create","entity":"participant"}' in (
        failed.value.issues[0].message
    )


@pytest.mark.anyio
async def test_chromium_acceptance_uses_fresh_reveal_budget_for_each_entity() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { runAction, useRecords } from "@dot/app-runtime";
import { AppShell, Button, Input } from "@dot/ui";
export default function App() {
  const { records: firstRecords } = useRecords<{ id: string; name: string }>("first_item");
  const { records: secondRecords } = useRecords<{ id: string; name: string }>("second_item");
  const [phase, setPhase] = useState<"first" | "second">("first");
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const entity = phase === "first" ? "first_item" : "second_item";
  return <AppShell title="Two steps">
    {!open ? <Button onClick={() => setOpen(true)}>add item</Button> : null}
    {open ? <form onSubmit={async (event) => {
      event.preventDefault();
      await runAction("records.create", { entity, data: { name } });
      setName("");
      setOpen(false);
      if (phase === "first") setPhase("second");
    }}>
      <Input label="Name" name="name" value={name} onValueChange={setName} />
      <Button type="submit">save item</Button>
    </form> : null}
    <p>{firstRecords.length} first, {secondRecords.length} second</p>
  </AppShell>;
}'''
        )
    )
    plan = tuple(
        {
            "operation": "records.create",
            "entity": entity,
            "required": True,
            "event_type": "submit",
            "field_hints": {"name": "test"},
            "required_payload_fields": ["name"],
            "allowed_payload_fields": ["name"],
        }
        for entity in ("first_item", "second_item")
    )

    result = await runner.smoke(bundle, acceptance_plan=plan)

    mutations = [item for item in result["operations"] if item["mutating"]]
    assert [item["args"]["entity"] for item in mutations] == ["first_item", "second_item"]
    assert result["workflow_reveals"] == [
        {"entity": "first_item", "label": "add item", "matched": True},
        {"entity": "second_item", "label": "add item", "matched": True},
    ]


@pytest.mark.anyio
async def test_chromium_acceptance_accepts_matching_direct_create_action() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { runAction, useRecords } from "@dot/app-runtime";
import { AppShell, Button } from "@dot/ui";
export default function App() {
  const { records } = useRecords<{ id: string; name: string }>("participant");
  return <AppShell title="Tripmates">
    <Button onClick={() => void runAction("records.create", {
      entity: "participant", data: { name: "Kareem" },
    })}>add participant</Button>
    <p>{records.map((record) => record.name).join(", ")}</p>
  </AppShell>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "participant",
            "required": True,
            "event_type": "submit",
            "field_hints": {"name": "test"},
            "required_payload_fields": ["name"],
            "allowed_payload_fields": ["name"],
        },
    )

    result = await runner.smoke(bundle, acceptance_plan=plan)

    assert result["required_mutations_verified"] == 1
    assert result["record_refreshes_verified"] == 1
    assert result["persisted_renders_verified"] == 1
    assert result["workflow_reveals"] == [
        {
            "entity": "participant",
            "label": "add participant",
            "matched": True,
            "direct_action": True,
        }
    ]


@pytest.mark.anyio
async def test_chromium_acceptance_rejects_direct_action_for_wrong_entity() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { runAction } from "@dot/app-runtime";
import { AppShell, Button } from "@dot/ui";
export default function App() {
  return <AppShell title="Tripmates">
    <Button onClick={() => void runAction("records.create", {
      entity: "expense", data: { name: "Lunch" },
    })}>add participant</Button>
  </AppShell>;
}'''
        )
    )
    plan = (
        {
            "operation": "records.create",
            "entity": "participant",
            "required": True,
            "event_type": "submit",
            "field_hints": {"name": "test"},
            "required_payload_fields": ["name"],
            "allowed_payload_fields": ["name"],
        },
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle, acceptance_plan=plan)

    assert failed.value.issues[0].code == "acceptance_workflow_mismatch"
    assert "expected operation=records.create entity=participant" in failed.value.issues[0].message
    assert "observed operation=records.create entity=expense" in failed.value.issues[0].message
    assert "expense" in failed.value.issues[0].message


@pytest.mark.anyio
async def test_chromium_acceptance_reports_form_and_reveal_diagnostics() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { AppShell, Button } from "@dot/ui";
export default function App() {
  const [open, setOpen] = useState(false);
  return <AppShell title="Vote">
    <Button onClick={() => setOpen(true)}>show details</Button>
    {open ? <p>still no voting form</p> : null}
  </AppShell>;
}'''
        )
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle, acceptance_plan=_vote_plan())

    assert failed.value.issues[0].code == "acceptance_flow_missing"
    assert 'required=["choice"]' in failed.value.issues[0].message
    assert 'forms=[]' in failed.value.issues[0].message
    assert 'explored=["show details"]' in failed.value.issues[0].message


@pytest.mark.anyio
async def test_chromium_acceptance_allows_derived_required_field_without_control() -> None:
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
    <form
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
async def test_chromium_acceptance_rejects_ambiguous_marker_free_forms() -> None:
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
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    void runAction("records.create", { entity: "vote", data: { choice } });
  };
  return <AppShell title="Vote">
    <form onSubmit={submit}>
      <Input label="First choice" name="choice" value={choice} onValueChange={setChoice} />
      <Button type="submit">save first</Button>
    </form>
    <form onSubmit={submit}>
      <Input label="Second choice" name="choice" value={choice} onValueChange={setChoice} />
      <Button type="submit">save second</Button>
    </form>
  </AppShell>;
}'''
        )
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle, acceptance_plan=_vote_plan())

    assert failed.value.issues[0].code == "acceptance_flow_ambiguous"


@pytest.mark.anyio
async def test_chromium_acceptance_validates_observed_entity_without_markers() -> None:
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
  return <AppShell title="Vote"><form onSubmit={(event) => {
    event.preventDefault();
    void runAction("records.create", { entity: "poll", data: { choice } });
  }}>
    <Input label="Choice" name="choice" value={choice} onValueChange={setChoice} />
    <Button type="submit">save vote</Button>
  </form></AppShell>;
}'''
        )
    )

    with pytest.raises(AppBrowserSmokeError) as failed:
        await runner.smoke(bundle, acceptance_plan=_vote_plan())

    assert failed.value.issues[0].code == "acceptance_workflow_mismatch"
    assert "expected operation=records.create entity=vote" in failed.value.issues[0].message
    assert "observed operation=records.create entity=poll" in failed.value.issues[0].message


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
        (
            "ux_nested_label",
            '''export default function App() {
  return <main><h1>Trip planner</h1>
    <label>Outer label
      <label>Inner label
        <input style={{ width: 180, height: 44 }} />
      </label>
    </label>
  </main>;
}''',
        ),
        (
            "ux_overlapping_text",
            '''export default function App() {
  return <main>
    <h1>Trip planner</h1>
    <span style={{ position: "absolute", left: 16, top: 80, width: 140, height: 24 }}>
      Alpha copy
    </span>
    <span style={{ position: "absolute", left: 40, top: 80, width: 140, height: 24 }}>
      Beta copy
    </span>
  </main>;
}''',
        ),
        (
            "ux_leading_overflow",
            '''import { AppShell, Item, List } from "@dot/ui";
export default function App() {
  return <AppShell title="Prep">
    <List>
      <Item
        leading={<span>Mark complete now please extra</span>}
        title="Passport"
        meta="Open"
      />
    </List>
  </AppShell>;
}''',
        ),
        (
            "ux_giant_heading",
            '''export default function App() {
  return <main className="dot-app-shell" data-density="compact">
    <header className="dot-app-header">
      <h1 style={{ fontSize: 32 }}>Portugal Trip Hub</h1>
    </header>
  </main>;
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
    <button style={{ width: 120, height: 44 }}>Add guest</button>
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


@pytest.mark.anyio
async def test_chromium_acceptance_persists_checkbox_toggle() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { runAction, useRecords } from "@dot/app-runtime";
import { AppShell, Checkbox, Item, List } from "@dot/ui";
type Row = { id: string; version: number; text: string; completed: boolean };
export default function App() {
  const { records, refresh } = useRecords<Row>("checklist_item");
  return <AppShell title="Prep">
    <List>{records.map((item) =>
      <Item key={item.id} title={item.text} meta="Open"
        leading={<Checkbox label="Done" checked={item.completed}
          onCheckedChange={(completed) => { void runAction("records.update", {
            record_id: item.id, expected_version: item.version, data: { completed },
          }).then(() => refresh()); }} />}
      />
    )}</List>
  </AppShell>;
}'''
        )
    )
    result = await runner.smoke(
        bundle,
        records={
            "checklist_item": [
                {
                    "id": "00000000-0000-4000-8000-000000000001",
                    "entity": "checklist_item",
                    "version": 1,
                    "text": "Check passport",
                    "completed": False,
                }
            ]
        },
    )

    updates = [
        operation
        for operation in result["operations"]
        if operation["operation"] == "records.update"
    ]
    assert len(updates) == 1
    assert updates[0]["args"]["data"] == {"completed": True}
    stored = result["records"]["checklist_item"][0]
    assert stored["completed"] is True
    assert stored["text"] == "Check passport"
    assert stored["version"] == 2
    assert result["ux_audit"]["passed"] is True


@pytest.mark.anyio
async def test_chromium_ux_audit_accepts_compact_appshell_and_item_checkbox() -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { useState } from "react";
import { AppShell, Checkbox, Field, Item, List } from "@dot/ui";
export default function App() {
  const [done, setDone] = useState(false);
  const [split, setSplit] = useState(false);
  return <AppShell title="Portugal Trip Hub">
    <List>
      <Item
        leading={<Checkbox label="Mark complete" checked={done} onCheckedChange={setDone} />}
        title="Check passport validity"
        detail="Kareem"
        meta="Open"
      />
    </List>
    <Field label="Split between">
      <Checkbox label="Alex" checked={split} onCheckedChange={setSplit} />
    </Field>
  </AppShell>;
}'''
        )
    )

    result = await runner.smoke(bundle)

    assert result["ux_audit"]["passed"] is True


@pytest.mark.anyio
async def test_chromium_acceptance_captures_at_rest_screenshot(tmp_path) -> None:
    runner = ChromiumAppAcceptanceRunner(timeout_seconds=12, sandbox_required=False)
    if not runner.available:
        pytest.skip("real Chromium acceptance runtime is unavailable")
    bundle = await EsbuildAppCompiler().compile(
        _source(
            '''import { AppShell, Item, List } from "@dot/ui";
export default function App() {
  return <AppShell title="Packing">
    <List>
      <Item title="Passport" meta="Packed" />
      <Item title="Charger" meta="Open" />
    </List>
  </AppShell>;
}'''
        )
    )
    screenshot_path = tmp_path / "at-rest.jpeg"

    result = await runner.smoke(bundle, screenshot_path=screenshot_path)

    assert result["screenshot"] is True
    contents = screenshot_path.read_bytes()
    assert contents[:2] == b"\xff\xd8"
    assert len(contents) > 1_000
