"""Frozen simplified inputs share the production normalization and receipt path."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.browser.browser_copy import CAPTURE, CopyTurn
from codey.operations.kernel import turn_inputs
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.kernel.turn_contract import TurnSnapshot
from codey.operations.kernel.turn_presentation import refresh_snapshot_contract
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, canonical_json_object, decode_json_object, parse_json_object

URL = "https://example.com/input-binding"
GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact", "browser.effect",
                    "project.read", "project.write"})


def upload_snapshot() -> TurnSnapshot:
    session = TaskSession(TaskPolicy(grants=GRANTS), task_kind="chat", task_text=f"上传附件到 {URL}",
                          required_source_urls=frozenset({URL}))
    return build_turn_snapshot(session)


def copy_snapshot(*, table: bool = False, business_key: str = "RZ-824") -> TurnSnapshot:
    session = TaskSession(TaskPolicy(grants=GRANTS), task_kind="hybrid",
        task_text=(f"把 {URL} 中 {business_key} 的 Balance 原样写入 vouchers.json 的 outstanding，整数。" if table else
                   f"把 {URL} 的运单号原样写入 dispatch.json 的 carrier_label，保留前导零。"))
    data: JsonObject = ({"tables": [{"id": "table-1", "columns": ["Batch", "Balance"],
                                    "rows": [[business_key, "962"]], "rectangular": True}], "records": []} if table else
                       {"tables": [], "records": [{"id": "json-1", "title": "shipment",
                                                   "text": '{"parcel":{"label":"000783"}}'}]})
    session._memory_results["capture"] = ToolResult(ToolCall("browser_eval", {"code": CAPTURE}, call_id="observation:child:0"), "captured",
        ok=True, canonical={"value": {"value": data, "truncated": False}})
    session._memory_results["parent"] = ToolResult(ToolCall("codemode", {}), "observed", ok=True,
        canonical={"child_effect_ids": ["capture"]}, audit={"codemode_execution_id": "observation"})
    return build_turn_snapshot(session)


@pytest.mark.parametrize("args", [{"code": "return null;"}, {"repair_of": "cmr_12345678"},
                                   {"upload": {"action": "observe"}, "code": "return null;"}])
def test_upload_rejects_undeclared_input_before_execution(args: JsonObject) -> None:
    snapshot = upload_snapshot()
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("original", "codemode", args),)),
                          snapshot=snapshot)
    assert plan.protocol_error
    assert not plan.calls


@pytest.mark.parametrize("kind", ["upload", "browser_copy"])
def test_binding_freezes_one_model_contract_and_preserves_native_identity(kind: str) -> None:
    snapshot = upload_snapshot() if kind == "upload" else copy_snapshot()
    binding = snapshot.input_binding
    assert binding is not None and binding.kind == kind
    model = next(spec for spec in snapshot.model_specs if spec.name == "codemode")
    execution = next(spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    assert model is binding.model_spec
    assert next(spec for spec in snapshot.model_specs if spec.name == "codemode") is model
    assert "code" not in dict(model.parameters)
    execution_parameters: dict[str, object] = dict(execution.parameters)
    assert "code" in execution_parameters and "upload" not in execution_parameters
    args: JsonObject = ({"upload": {"action": "observe"}} if kind == "upload" else {
        "source": {"pointer": "/parcel/label"}})
    native = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("original", "codemode", args),)),
                            snapshot=snapshot)
    text_payload: JsonObject = {"tool": "codemode", "args": args}
    text = normalize_turn(decode_text_turn(json.dumps(text_payload)), snapshot=snapshot)
    assert not native.protocol_error and not text.protocol_error
    assert native.calls[0].call_id == "original"
    assert native.calls[0].args == text.calls[0].args
    definitions = tools_from_specs(snapshot.model_specs)
    parameters = next(tool.parameters for tool in definitions if tool.name == "codemode")
    assert "code" not in decode_json_object(canonical_json_object(parameters)["properties"])
    restored = restore_snapshot(parse_json_object(json.dumps(snapshot_description(snapshot), sort_keys=True)))
    assert restored == snapshot
    assert normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("original", "codemode", args),)),
                          snapshot=restored).calls == native.calls


@pytest.mark.parametrize("key", ["RZ-824", "lot_17"])
def test_copy_only_requests_unbound_source_fields_and_restores_them(key: str) -> None:
    snapshot = copy_snapshot(table=True, business_key=key)
    binding = snapshot.input_binding
    assert binding is not None
    model = binding.model_spec
    assert model.required == ("source",) and set(dict(model.parameters)) == {"source"}
    source = canonical_json_object(dict(model.parameters)["source"])
    expected = {"key_column", "column"} | ({"key"} if key == "lot_17" else set())
    assert set(decode_json_object(source["properties"])) == expected
    assert "vouchers.json" in model.description and "outstanding" in model.description
    if key == "RZ-824":
        assert key in model.description
    selector: JsonObject = {"key_column": "Batch", "column": "Balance"}
    if key == "lot_17":
        selector["key"] = key
    args: JsonObject = {"source": selector}
    restored = restore_snapshot(parse_json_object(json.dumps(snapshot_description(snapshot), sort_keys=True)))
    assert restored == snapshot
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("original", "codemode", args),)), snapshot=restored)
    assert not plan.protocol_error and len(plan.calls) == 1
    inputs = decode_json_object(plan.calls[0].args["inputs"])
    assert inputs["path"] == "vouchers.json"
    lowered_source = decode_json_object(inputs["source"])
    assert decode_json_object(lowered_source["selector"]) == {**selector, "id": "table-1", "key": key}
    assert args == {"source": selector}


@pytest.mark.parametrize("old_field", ["target", "key"])
def test_changed_copy_definition_without_matching_contract_text_is_rejected(old_field: str) -> None:
    payload = snapshot_description(copy_snapshot(table=True))
    binding = decode_json_object(payload["input_binding"])
    specs = payload["model_specs"]
    assert isinstance(specs, list)
    index = next(i for i, row in enumerate(specs) if decode_json_object(row)["name"] == binding["tool"])
    spec = decode_json_object(specs[index])
    parameters = spec["parameters"]
    assert isinstance(parameters, list)
    if old_field == "target":
        parameters.append(["target", {"type": "object", "properties": {
            "file": {"type": "string", "enum": ["vouchers.json"]},
            "field": {"type": "string", "enum": ["outstanding"]}},
            "required": ["file", "field"], "additionalProperties": False}])
        spec["required"] = ["source", "target"]
    else:
        pair = parameters[0]
        assert isinstance(pair, list)
        source = decode_json_object(pair[1])
        properties = decode_json_object(source["properties"])
        properties["key"] = {"type": "string", "enum": ["RZ-824"]}
        source["properties"] = properties
        source["required"] = ["key_column", "key", "column"]
        parameters[0] = ["source", source]
    specs[index] = spec
    with pytest.raises(ValueError, match="contract text differs"):
        restore_snapshot(payload)


def test_invalid_batch_discards_previously_lowered_upload() -> None:
    plan = normalize_turn(AssistantTurn(tool_calls=(
        ProviderToolCall("valid", "codemode", {"upload": {"action": "observe"}}),
        ProviderToolCall("invalid", "codemode", {"code": "return null;"}),
    )), snapshot=upload_snapshot())
    assert plan.protocol_error and not plan.calls


def test_fixed_template_syntax_failure_uses_the_model_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = upload_snapshot()
    assert snapshot.policy is not None
    session = TaskSession(snapshot.policy, task_kind="chat", task_text=f"上传附件到 {URL}")

    def broken_template(_args: JsonObject, _checks: tuple[str, ...] = ()) -> JsonObject:
        return {"code": "return (", "inputs": {}}

    monkeypatch.setattr(turn_inputs, "lower_upload_action", broken_template)
    plan = normalize_turn(AssistantTurn(tool_calls=(
        ProviderToolCall("original", "codemode", {"upload": {"action": "observe"}}),
    )), snapshot=snapshot)
    assert not plan.protocol_error
    executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="syntax",
        effect_scope="task", turn=0, project_path=tmp_path)
    result, = execute_turn(session, plan.calls, snapshot=snapshot, executors={"codemode": executor},
        run_id="syntax", effect_scope="task", turn=0, project_path=tmp_path)
    assert not result.ok and result.audit["error_code"] == "syntax_error"
    assert result.canonical["child_effect_ids"] == []
    assert '"upload"' in result.model_text and '"code"' not in result.model_text
    assert "fixed runtime template failed" in result.model_text
    assert "repair_of" not in result.model_text and "Syntax repair required" not in result.model_text
    assert "Put multiline or quoted task text in inputs" not in result.model_text
    assert "SyntaxError" in str(result.audit["template_error"])


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_old_request_snapshots_are_rejected(version: int) -> None:
    payload = snapshot_description(upload_snapshot())
    payload["schema_version"] = version
    with pytest.raises(ValueError, match="version"):
        restore_snapshot(payload)


def test_copy_observation_is_detached_and_readonly_between_uses() -> None:
    snapshot = copy_snapshot()
    assert snapshot.browser_copy is not None
    original = snapshot.browser_copy.payload()
    copy = CopyTurn.restore(original)
    original["data"] = {"tables": list[JsonValue](), "records": list[JsonValue]()}
    exposed = copy.data
    assert exposed is not None
    exposed["records"] = list[JsonValue]()
    assert copy.data != exposed
    assert copy == snapshot.browser_copy


def test_restored_binding_cannot_change_authorization() -> None:
    payload = snapshot_description(upload_snapshot())
    binding = decode_json_object(payload["input_binding"])
    specs = payload["model_specs"]
    assert isinstance(specs, list)
    index = next(i for i, row in enumerate(specs) if decode_json_object(row)["name"] == binding["tool"])
    spec = decode_json_object(specs[index])
    spec["grant"] = "project.write"
    specs[index] = spec
    with pytest.raises(ValueError, match="authority"):
        restore_snapshot(payload)


def test_hidden_bound_tool_is_not_converted() -> None:
    original = upload_snapshot()
    snapshot = refresh_snapshot_contract(replace(original, tool_names=("done",), nested_tool_names=(),
        frozen_specs=tuple(spec for spec in original.frozen_specs if spec.name == "done"),
        custom_executors=(("done", None),)))
    assert snapshot.input_binding is None
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("hidden", "codemode", {}),)), snapshot=snapshot)
    assert plan.protocol_error_kind == "snapshot_tool_mismatch"
    assert not plan.calls
