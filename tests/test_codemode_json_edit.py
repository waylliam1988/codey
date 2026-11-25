from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.codemode.json_edit import JSON_FIELD_EDIT_PROGRAM, replace_json_fields
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.constants import STALE_READ_REFERENCE_ERROR
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from codey.workspace.revision import WorkspaceRevisionStore
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, Target


def _execute(session: TaskSession, project: Path, turn: int, code: str, *, repair_of: str = "",
             changes: JsonObject | None = None) -> ToolResult:
    snapshot = build_turn_snapshot(session)
    revisions = WorkspaceRevisionStore(project.parent / (project.name + "-revisions"))
    executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="json-edit", effect_scope="task",
        turn=turn, project_path=project, allow_writes=True, workspace_revision_store=revisions)
    args: JsonObject = {"code": code, "inputs": {"path": "preferences.json", "changes": (
        {"theme": "night"} if changes is None else changes)}}
    if repair_of:
        args["repair_of"] = repair_of
    return execute_turn(session, [ToolCall("codemode", args)], run_id="json-edit", effect_scope="task", turn=turn,
        project_path=project, executors={"codemode": executor}, snapshot=snapshot, workspace_revision_store=revisions)[0]


@pytest.mark.usefixtures("scripted_local_api_connection", "no_external_advisor_models")
def test_headless_json_field_edit_conflicts_then_preserves_the_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    file = project / "preferences.json"
    baseline = b'{ "theme": "old", "retry": 0, "keep": {"id":9007199254740993,"rate":0.1000000000000000001} }\n'
    external = baseline.replace(b' }\n', b', "added": {"owner":"outside"} }\n')
    expected = external.replace(b'"old"', b'"night"').replace(b'"retry": 0', b'"retry": 7')
    file.write_bytes(baseline)
    sibling = project / "other.json"
    sibling.write_bytes(baseline)
    after_edits: list[bytes] = []
    injected = False

    def observe(event: dict[str, object]) -> None:
        nonlocal injected
        if event.get("type") == "tool":
            if event.get("tool_name") == "read_file" and event.get("ok") is True and not injected:
                file.write_bytes(external)
                injected = True
            elif event.get("tool_name") == "edit":
                after_edits.append(file.read_bytes())

    edit: JsonObject = {"tool": "codemode", "args": {
        "code": JSON_FIELD_EDIT_PROGRAM, "inputs": {"path": file.name, "changes": {"theme": "night", "retry": 7}},
    }}
    install_http(monkeypatch, [edit, edit, {"tool": "done", "args": {"summary": "Updated preferences."}}])
    monkeypatch.setattr("codey.app.provider_services.provider_availability", lambda: {"local": True})

    def review_unavailable(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("codey.app.review_service.run_review", review_unavailable)
    target = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10)
    provider = ExperimentProvider(target, tmp_path, fixed_acquisition=False)
    state = tmp_path / "state"

    def connect(_provider_id: str, **_kwargs: object) -> ExperimentProvider:
        return provider

    selection: JsonObject = {"model": target.model, "base_url": target.base_url}
    try:
        result = run_headless(HeadlessRequest(project, "Update the theme and retry count in preferences.json. Do not run tests or commands.",
            intent="project", provider_id="local", max_turns=3, state_home=state,
            research_store_root=tmp_path / "knowledge", allow_web=False, project_changes_required=True,
            required_tools=(require_tool_success("edit", via="codemode"),),
            model_selection=selection),
            emit_jsonl=observe, connect_provider=connect)
    finally:
        provider.close()
    assert result.stop_reason == "done"
    assert after_edits == [external, expected]
    assert file.read_bytes() == expected and sibling.read_bytes() == baseline
    receipts = tool_receipts(state)
    reads = [r.identity for r in receipts if r.result.call.name == "read_file"]
    edits = [r.result for r in receipts if r.result.call.name == "edit"]
    assert len(reads) == len(edits) == 2
    assert [r.call.args["base_result_ref"] for r in edits] == reads
    assert not edits[0].ok and STALE_READ_REFERENCE_ERROR in edits[0].model_text and edits[1].ok
    assert edits[1].call.args["content"] == expected.decode("utf-8")
    parents = [r.result for r in receipts if r.result.call.name == "codemode"]
    assert not parents[0].ok and parents[1].ok
    feedback = parents[0].model_text.split("Current codemode contract:")[0]
    assert "rerun the same two lines" in feedback and "unwrap only the edit result" in feedback
    requests = [parse_json_object(line) for line in
                (tmp_path / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any("tools.edit({base_result_ref: read.result_ref, changes: inputs.changes})" in str(message.get("content", ""))
               for row in requests if row.get("phase") == "request"
               if isinstance(messages := decode_json_object(row["payload"]).get("messages"), list)
               for message in messages if isinstance(message, dict) and message.get("role") == "user")


@pytest.mark.parametrize(("text", "read_options", "arguments", "error"), [
    ('{"theme":"old"}\n', "", "{base_result_ref: 'forged', changes: inputs.changes}", "original complete"),
    ('{"theme":"old"}\n', "", "{path: inputs.path, base_result_ref: read.result_ref, changes: inputs.changes}", "original complete"),
    ('{\n"theme":"old"\n}\n', ", limit: 1", "{base_result_ref: read.result_ref, changes: inputs.changes}", "original complete"),
    ('{"other":"old"}\n', "", "{base_result_ref: read.result_ref, changes: inputs.changes}", "fields do not exist"),
    ('{"theme":"old","theme":"duplicate"}\n', "", "{base_result_ref: read.result_ref, changes: inputs.changes}", "duplicate JSON field"),
    ('["old"]\n', "", "{base_result_ref: read.result_ref, changes: inputs.changes}", "file must contain a JSON object"),
])
def test_invalid_json_field_edit_does_not_dispatch_write(
    tmp_path: Path, text: str, read_options: str, arguments: str, error: str,
) -> None:
    file = tmp_path / "preferences.json"
    file.write_bytes(text.encode("utf-8"))
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(tmp_path))
    code = (f"const read = await tools.read_file({{path: inputs.path{read_options}}}); "
            f"return unwrap(await tools.edit({arguments}));")
    result = _execute(session, tmp_path, 1, code)
    assert not result.ok and error in result.model_text
    assert not any(receipt.call.name == "edit" for receipt in session._memory_results.values())
    assert file.read_bytes() == text.encode("utf-8")


@pytest.mark.parametrize("wrapper", ["value", "payload"])
@pytest.mark.parametrize("fields", [("amount", "code"), ("weight", "seal")])
def test_extra_json_layer_identifies_target_and_fields_without_writing(
    tmp_path: Path, wrapper: str, fields: tuple[str, str],
) -> None:
    first, second = fields
    text = f'{{"{first}":0,"{second}":"old","keep":9007199254740993}}\n'
    file = tmp_path / "preferences.json"
    file.write_bytes(text.encode("utf-8"))
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(tmp_path))
    changes: JsonObject = {wrapper: {first: 173, second: "SEAL_492"}}
    failed = _execute(session, tmp_path, 1, JSON_FIELD_EDIT_PROGRAM, changes=changes)
    assert not failed.ok
    feedback = failed.model_text
    assert "target file 'preferences.json'" in feedback
    nested_fields: list[str] = sorted(fields)
    existing_fields: list[str] = sorted([*fields, "keep"])
    assert f'changes["{wrapper}"] wraps existing fields {json.dumps(nested_fields)}' in feedback
    assert f'Existing top-level fields: {json.dumps(existing_fields)}' in feedback
    assert f'Submitted fields: ["{wrapper}"]' in feedback
    assert not any(result.call.name == "edit" for result in session._memory_results.values())
    assert file.read_bytes() == text.encode("utf-8")


def test_real_value_field_is_editable_and_unrelated_nested_keys_get_no_wrapper_hint(tmp_path: Path) -> None:
    file = tmp_path / "preferences.json"
    file.write_bytes(b'{"value":{"old":0},"keep":9007199254740993}\n')
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(tmp_path))
    result = _execute(session, tmp_path, 1, JSON_FIELD_EDIT_PROGRAM, changes={"value": {"amount": 173, "code": "SEAL_492"}})
    assert result.ok, result.model_text
    assert parse_json_object(file.read_text(encoding="utf-8")) == {
        "value": {"amount": 173, "code": "SEAL_492"}, "keep": 9007199254740993}
    with pytest.raises(ValueError) as error:
        replace_json_fields('{"amount":0,"code":"old"}', {"value": {"unknown": 173}})
    assert "extra object layer" not in str(error.value)


def test_return_only_repair_of_json_field_edit_reuses_receipts_without_writing(tmp_path: Path) -> None:
    file = tmp_path / "preferences.json"
    file.write_bytes(b'{"theme":"day","other":0}\n')
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(tmp_path))
    failed = _execute(session, tmp_path, 1, JSON_FIELD_EDIT_PROGRAM.replace("return unwrap", "unwrap"))
    assert not failed.ok and failed.audit["error_code"] == "missing_return"
    assert file.read_bytes() == b'{"theme":"night","other":0}\n'
    external = b'{"theme":"night","other":17}\n'
    file.write_bytes(external)
    repair = decode_json_object(failed.canonical["repair"])
    token = require_json_string(repair["token"], "repair token")
    recovered = _execute(session, tmp_path, 2, JSON_FIELD_EDIT_PROGRAM, repair_of=token)
    assert recovered.ok, recovered.model_text
    assert recovered.audit["recovery_disposition"] == "receipt_replay"
    assert file.read_bytes() == external
    assert sum(receipt.call.name == "edit" for receipt in session._memory_results.values()) == 1
