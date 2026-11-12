"""Frozen simplified inputs share the production normalization and receipt path."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.operations import kernel_protocol
from codey.operations.browser_copy import CAPTURE, CopyTurn
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import (
    TurnSnapshot,
    build_turn_snapshot,
    normalize_turn,
    refresh_snapshot_contract,
)
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object, parse_json_object

URL = "https://example.com/input-binding"
GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact", "browser.effect",
                    "project.read", "project.write"})


def upload_snapshot() -> TurnSnapshot:
    session = TaskSession(TaskPolicy(grants=GRANTS), task_kind="chat", task_text=f"上传附件到 {URL}",
                          required_source_urls=frozenset({URL}))
    return build_turn_snapshot(session)


def copy_snapshot() -> TurnSnapshot:
    session = TaskSession(TaskPolicy(grants=GRANTS), task_kind="hybrid",
                          task_text=f"把 {URL} 的运单号原样写入 dispatch.json 的 carrier_label，保留前导零。")
    data: JsonObject = {"tables": [], "records": [{"id": "json-1", "title": "shipment",
                         "text": '{"parcel":{"label":"000783"}}'}]}
    session._memory_results["capture"] = ToolResult(ToolCall("browser_eval", {"code": CAPTURE}), "captured",
        ok=True, canonical={"value": {"value": data, "truncated": False}})
    session._memory_results["parent"] = ToolResult(ToolCall("codemode", {}), "observed", ok=True,
                                                  canonical={"child_effect_ids": ["capture"]})
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
        "source": {"pointer": "/parcel/label"}, "target": {"file": "dispatch.json", "field": "carrier_label"}})
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

    def broken_template(_args: JsonObject) -> JsonObject:
        return {"code": "return (", "inputs": {}}

    monkeypatch.setattr(kernel_protocol, "lower_upload_action", broken_template)
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
    original["data"] = {"tables": [], "records": []}
    exposed = copy.data
    assert exposed is not None
    exposed["records"] = []
    assert copy.data != exposed
    assert copy == snapshot.browser_copy


def test_restored_binding_cannot_change_authorization() -> None:
    payload = snapshot_description(upload_snapshot())
    binding = decode_json_object(payload["input_binding"])
    spec = decode_json_object(binding["model_spec"])
    spec["grant"] = "project.write"
    payload["input_binding"] = {**binding, "model_spec": spec}
    with pytest.raises(ValueError, match="binding"):
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
