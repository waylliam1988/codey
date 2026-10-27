"""Initial and repair views use one frozen schema without catalog repetition."""
import json
from dataclasses import replace

import pytest

from codey.operations.kernel_prompt import _repair_prompt
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs
from codey.runtime.core.models import ToolPlan
from codey.toolchain import tool_spec
from codey.toolchain.tool_prompt import json_contract_text


def test_each_tool_has_one_usage_and_keeps_every_original_example():
    examples = tuple(json.dumps({"tool": "probe", "args": {"query": value}})
                     for value in ('bare', '"quoted"', 'line\nnext'))
    spec = tool_spec.freeze_spec_parameters(tool_spec.ToolSpec(
        name="probe", description="UNIQUE_USAGE", required=("query",),
        parameters=(("query", {"type": "string"}), ("limit", {"type": "integer", "minimum": 1})),
        json_examples=examples, declarations="UNIQUE_GENERATED_DECLARATIONS"))
    text = json_contract_text((spec,))
    assert text.count(spec.description) == 1
    assert text.count(spec.declarations) == 1
    assert '"query": string' in text and '"limit"?: number' in text
    assert all(text.count(example) == 1 for example in examples)


def test_native_usage_and_generated_catalog_are_joined_only_at_rendering():
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control", "project.read"}))), native=True)
    spec = next(item for item in snapshot.frozen_specs if item.name == "codemode")
    wire = next(item for item in tools_from_specs(snapshot.frozen_specs) if item.name == "codemode")
    assert spec.declarations and "declare const tools" not in spec.description
    assert wire.description == spec.description + "\n" + spec.declarations
    assert wire.description.count("declare const tools") == 1


@pytest.mark.parametrize("native", [False, True])
def test_argument_repair_is_self_contained_and_uses_only_the_failing_frozen_tool(monkeypatch, native):
    state = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})))
    snapshot = build_turn_snapshot(state, native=native)
    args = {"query": 'literal "Q"\nnext', "include": ["*.py", "*.ts"]}
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("invalid", "grep", args),)), snapshot=snapshot)
    assert not plan.calls and plan.protocol_tool_name == "grep"
    captured = next(spec for spec in snapshot.frozen_specs if spec.name == "grep")
    monkeypatch.setitem(tool_spec.tool_specs(), "grep", replace(captured, description="UNSENT_DESCRIPTION"))
    monkeypatch.setattr(tool_spec, "tool_specs", lambda: pytest.fail("repair read the live registry"))
    prompt = _repair_prompt(plan, snapshot=snapshot, native=native)
    if native:
        wire = next(item for item in tools_from_specs(snapshot.frozen_specs) if item.name == "grep")
        schema = tool_spec.thaw_schema_value(wire.parameters)
        assert "not an array" in prompt and "*.{py,ts}" in prompt
        assert "required arguments: query" in prompt and "JSON Schema" not in prompt
        assert wire.description == captured.description
    else:
        encoded = prompt.split("Arguments (JSON Schema; only declared fields): ")[1].splitlines()[0]
        schema = json.loads(encoded)
        assert prompt.count(captured.description) == 1
    assert schema == tool_spec.thaw_schema_value(tool_spec._schema_for_spec(captured))
    assert schema["required"] == ["query"] and schema["additionalProperties"] is False
    assert "UNSENT_DESCRIPTION" not in prompt and "Current edit contract" not in prompt
    assert "catalog_digest" not in prompt and "summary string" not in prompt
    assert args == {"query": 'literal "Q"\nnext', "include": ["*.py", "*.ts"]}
    assert not state.executed


def test_unidentified_error_offers_only_captured_signatures_without_guessing_the_tool():
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control", "project.read"}))))
    prompt = _repair_prompt(ToolPlan(calls=[], control=None, protocol_error="bad framing near edit"), snapshot=snapshot, native=False)
    assert "Available tool signatures" in prompt and "read_file(args:" in prompt
    assert "edit(args:" not in prompt and "Current edit contract" not in prompt
    assert "Example:" not in prompt and "catalog_digest" not in prompt
    assert '"tool":"ACTUAL_TOOL_NAME"' in prompt
