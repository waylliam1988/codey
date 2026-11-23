"""Initial and repair views use one frozen schema without catalog repetition."""
import json
from dataclasses import replace

import pytest

from codey.operations.kernel_prompt import _repair_prompt, _text_request_prompt
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs
from codey.providers.text_tool_schema import text_tool_schema
from codey.runtime.core.models import ToolPlan
from codey.toolchain import tool_spec
from codey.toolchain.tool_prompt import json_contract_text
from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    decode_json_object,
    parse_json_object,
    require_json_strings,
)


def test_each_tool_has_one_usage_and_keeps_every_original_example() -> None:
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


def test_native_usage_and_generated_catalog_are_joined_only_at_rendering() -> None:
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control", "project.read"}))), native=True)
    spec = next(item for item in snapshot.frozen_specs if item.name == "codemode")
    wire = next(item for item in tools_from_specs(snapshot.frozen_specs) if item.name == "codemode")
    assert spec.declarations and "declare const tools" not in spec.description
    assert wire.description == spec.description + "\n" + spec.declarations
    assert wire.description.count("declare const tools") == 1


def test_schema_text_projects_frozen_tools_and_keeps_plain_text_examples() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), task_kind="project")
    snapshot = build_turn_snapshot(session)
    specs = snapshot.model_specs
    grep = next(spec for spec in specs if spec.name == "grep")
    tools = tools_from_specs(specs)
    schema = decode_json_object(text_tool_schema(tools))
    branches = schema["oneOf"]
    assert isinstance(branches, list)
    names: set[str] = set()
    for branch in branches:
        parts = decode_json_object(branch)["allOf"]
        assert isinstance(parts, list)
        properties = decode_json_object(decode_json_object(parts[0])["properties"])
        names.update(require_json_strings(decode_json_object(properties["tool"])["enum"], "tool names"))
    assert names == set(snapshot.tool_names)
    plain = _text_request_prompt("", snapshot)
    constrained = _text_request_prompt("", snapshot, schema_constrained=True)
    assert all(example in plain and example not in constrained for example in grep.json_examples)
    assert f"- grep: {grep.description}" in constrained and "grep(args:" in constrained
    assert all(f"- {tool.name}:" in constrained for tool in tools)
    restored = restore_snapshot(snapshot_description(snapshot))
    assert _text_request_prompt("", restored, schema_constrained=True) == constrained


@pytest.mark.parametrize("native", [False, True])
def test_argument_repair_is_self_contained_and_uses_only_the_failing_frozen_tool(
    monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    state = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})))
    snapshot = build_turn_snapshot(state, native=native)
    args: JsonObject = {"query": 'literal "Q"\nnext', "include": ["*.py", "*.ts"]}
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("invalid", "grep", args),)), snapshot=snapshot)
    assert not plan.calls and plan.protocol_tool_name == "grep"
    captured = next(spec for spec in snapshot.frozen_specs if spec.name == "grep")
    monkeypatch.setitem(tool_spec.tool_specs(), "grep", replace(captured, description="UNSENT_DESCRIPTION"))
    monkeypatch.setattr(tool_spec, "tool_specs", lambda: pytest.fail("repair read the live registry"))
    prompt = _repair_prompt(plan, snapshot=snapshot, native=native)
    if native:
        wire = next(item for item in tools_from_specs(snapshot.frozen_specs) if item.name == "grep")
        schema = canonical_json_object(wire.parameters)
        assert "not an array" in prompt and "*.{py,ts}" in prompt
        assert "required arguments: query" in prompt and "JSON Schema" not in prompt
        assert wire.description == captured.description
    else:
        encoded = prompt.split("Arguments (JSON Schema; only declared fields): ")[1].splitlines()[0]
        schema = parse_json_object(encoded)
        assert prompt.count(captured.description) == 1
    assert schema == tool_spec.thaw_schema_value(tool_spec._schema_for_spec(captured))
    assert schema["required"] == ["query"] and schema["additionalProperties"] is False
    assert "UNSENT_DESCRIPTION" not in prompt and "Current edit contract" not in prompt
    assert "catalog_digest" not in prompt and "summary string" not in prompt
    assert args == {"query": 'literal "Q"\nnext', "include": ["*.py", "*.ts"]}
    assert not state.executed


def test_unidentified_error_offers_only_captured_signatures_without_guessing_the_tool() -> None:
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control", "project.read"}))))
    prompt = _repair_prompt(ToolPlan(calls=[], control=None, protocol_error="bad framing near edit"), snapshot=snapshot, native=False)
    assert "Available tool signatures" in prompt and "read_file(args:" in prompt
    assert "edit(args:" not in prompt and "Current edit contract" not in prompt
    assert "Example:" not in prompt and "catalog_digest" not in prompt
    assert '"tool":"ACTUAL_TOOL_NAME"' in prompt
