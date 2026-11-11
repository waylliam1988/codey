"""Structured entry counterexamples; no live model in these regression tests."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.codemode.runtime import NodeWorkerRuntime
from codey.codemode.types import ToolCatalogEntry
from codey.operations import completion_gate, completion_output
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import declaration
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.input_attachments import InputAttachmentStore
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_mechanical_surface import record
from tests.test_receipt_bound_edit_surface_probe import install_http
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime
from tools import receipt_bound_edit_surface_probe as probe
from tools import structured_surface as structured
from tools.browser_production_live_gate import URL
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target
from tools.receipt_bound_edit_recovery_probe import EXTERNAL_FIELD


def session() -> TaskSession:
    return TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write", "web.read",
                                            "browser.observe", "browser.interact", "browser.effect"})))


def step(identity: str, op: str, args: JsonObject, *, allow_failure: bool = False) -> JsonObject:
    value: JsonObject = {"id": identity, "op": op, "args": args}
    if allow_failure:
        value["allow_failure"] = True
    return value


def plan(*steps: JsonObject) -> JsonObject:
    return {"tool": "codemode", "args": {"steps": list(steps)}}


def browser_steps() -> list[JsonObject]:
    return [step("nav", "browser_navigate", {"url": URL}),
            step("fill", "browser_fill", {"name": "Locality", "value": "福州"}),
            step("click", "browser_click", {"name": "Load record"}),
            step("wait", "browser_wait", {"condition": "text", "value": "鱼丸"}),
            step("body", "browser_read", {})]


class State:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def load(self, key: str) -> object:
        return self.values.get(key)

    def store(self, key: str, value: object) -> None:
        self.values[key] = value


@pytest.mark.parametrize("defect", ["condition", "timeout", "unknown", "javascript", "duplicate", "property", "extra"])
def test_whole_plan_is_validated_before_execution(defect: str) -> None:
    snapshot = build_turn_snapshot(session())
    schemas = structured.entry_schemas(snapshot)
    args = decode_json_object(plan(*browser_steps())["args"])
    steps = args["steps"]
    assert isinstance(steps, list)
    if defect == "condition":
        steps.append(step("bad", "browser_wait", {"value": "ready"}))
    elif defect == "timeout":
        steps.append(step("bad", "browser_wait", {"condition": "text", "value": "ready", "timeout_ms": True}))
    elif defect == "unknown":
        steps.append(step("bad", "shell", {"command": "echo unsafe"}))
    elif defect == "javascript":
        args = {"code": "return 1;"}
    elif defect == "duplicate":
        steps.append(step("nav", "browser_read", {}))
    elif defect == "property":
        steps.append(step("bad", "browser_property", {"name": "__token;alert(1)"}))
    else:
        steps.append(step("bad", "browser_read", {"undeclared": "field"}))
    with pytest.raises(ValueError) as rejected:
        structured.compile_plan(args, schemas)
    expected = {"condition": "condition", "timeout": "timeout_ms", "unknown": "steps[5].op", "javascript": "steps",
                "duplicate": "duplicate step id", "property": "must match", "extra": "undeclared"}
    assert expected[defect] in str(rejected.value)


def test_no_plan_operation_exceeds_frozen_permissions() -> None:
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control", "project.read"}))))
    schemas = structured.entry_schemas(snapshot)
    assert "read_file" in schemas
    assert not {"edit", "browser_property", "browser_upload"} & schemas.keys()


@pytest.mark.parametrize("stale_refs", [False, True])
def test_named_targets_are_mandatory_and_old_snapshots_are_diagnostic_only(stale_refs: bool) -> None:
    schemas = structured.entry_schemas(build_turn_snapshot(session()), stale_refs=stale_refs)
    for op in structured.ACTION_TOOLS:
        properties = decode_json_object(schemas[op]["properties"])
        assert "ref" not in properties and "name" in schemas[op]["required"]
        assert ("snapshot_id" in properties) is stale_refs
    with pytest.raises(ValueError):
        structured.compile_plan({"steps": [step("click", "browser_click", {"ref": "guessed"})]}, schemas)
    old = {"steps": [step("click", "browser_click", {"name": "Load record", "snapshot_id": "before"})]}
    if stale_refs:
        structured.compile_plan(old, schemas)
    else:
        with pytest.raises(ValueError, match="snapshot_id"):
            structured.compile_plan(old, schemas)


def test_wait_default_is_explicit_and_interpreter_returns_results() -> None:
    compiled = structured.compile_plan(decode_json_object(plan(step("wait", "browser_wait", {
        "condition": "text", "value": "ready"}))["args"]), structured.entry_schemas(build_turn_snapshot(session())))
    calls: list[dict[str, object]] = []

    def invoke(name: str, args: dict[str, object]) -> object:
        calls.append(args)
        return {"ok": True, "value": {"matched": True}}

    result = NodeWorkerRuntime().execute(str(compiled["code"]), inputs=decode_json_object(compiled["inputs"]),
        catalog=[ToolCatalogEntry("browser_wait", "Wait")], invoke=invoke, state_store=State())
    assert result.ok, result.error
    assert calls == [{"condition": "text", "value": "ready", "timeout_ms": 5000}]


@pytest.mark.parametrize("matches", [0, 2])
def test_ambiguous_target_performs_no_action(matches: int) -> None:
    compiled = structured.compile_plan(decode_json_object(plan(step("upload", "browser_upload", {
        "name": "Attachment", "attachment_id": "att_" + "0" * 64}))["args"]),
        structured.entry_schemas(build_turn_snapshot(session())))
    calls: list[str] = []

    def invoke(name: str, args: dict[str, object]) -> object:
        calls.append(name)
        return {"ok": True, "value": {"elements": [{"name": "Attachment", "ref": str(i)} for i in range(matches)]}}

    result = NodeWorkerRuntime().execute(str(compiled["code"]), inputs=decode_json_object(compiled["inputs"]),
        catalog=[ToolCatalogEntry(name, name) for name in ("browser_snapshot", "browser_upload")], invoke=invoke, state_store=State())
    assert result.ok and "target_not_unique" in str(result.value)
    assert calls == ["browser_snapshot"]


def test_same_mutation_id_reuses_result_and_changed_arguments_are_rejected() -> None:
    schemas = structured.entry_schemas(build_turn_snapshot(session()))
    state = State()
    calls: list[str] = []

    def invoke(name: str, args: dict[str, object]) -> object:
        calls.append(name)
        if name == "browser_snapshot":
            return {"ok": True, "value": {"elements": [{"name": "Download", "ref": "real"}]}}
        return {"ok": True, "value": {"path": "actual", "sha256": "actual"}, "result_ref": "original"}

    for identity, target, expected_reuse in [("download", "Download", False), ("download", "Download", True),
                                          ("download", "Changed", False), ("intentional_second", "Download", False)]:
        compiled = structured.compile_plan(decode_json_object(plan(step(identity, "browser_download", {"name": target}))["args"]), schemas)
        result = NodeWorkerRuntime().execute(str(compiled["code"]), inputs=decode_json_object(compiled["inputs"]),
            catalog=[ToolCatalogEntry(name, name) for name in ("browser_snapshot", "browser_download")], invoke=invoke, state_store=state)
        assert result.ok
        value = decode_json_object(result.value)
        if identity == "download" and target == "Changed":
            assert "step_id_conflict" in str(value["error"])
        else:
            completed = value["completed"]
            assert isinstance(completed, list)
            assert decode_json_object(completed[0])["reused"] is expected_reuse
    assert calls == ["browser_snapshot", "browser_download", "browser_snapshot", "browser_download"]


@pytest.mark.parametrize("enabled", [False, True])
def test_durable_missing_parameter_keeps_root_cause_when_prechecked(tmp_path: Path, enabled: bool) -> None:
    project, state, logdir = _dirs(tmp_path)
    _, mutations, managed, revisions, _deps, sink = _open_runtime(logdir, state, "s", "r", project)
    s = TaskSession(TaskPolicy(frozenset({"control", "project.read", "browser.observe"})), project=str(project))
    from codey.operations.recovery import record_entry_policy

    record_entry_policy(mutations, session_id="s", run_id="r", policy=s.policy, durable=mutations.durable)
    try:
        with structured.prototype(tmp_path, "basic", enabled=enabled):
            snapshot = build_turn_snapshot(s)
            executor = build_execute_turn_executor(session=s, snapshot=snapshot, run_id="r", effect_scope="probe",
                turn=0, project_path=project, managed_outputs=managed, workspace_revision_store=revisions,
                intent_sink=sink, durable=mutations.durable, session_id="s")
            result = execute_turn(s, [ToolCall("codemode", {"code":
                "return unwrap(await tools.browser_wait({condition:'text',value:'ready'}));"}, call_id="parent")],
                executors={"codemode": executor}, run_id="r", effect_scope="probe", turn=0, project_path=project,
                snapshot=snapshot, intent_sink=sink, durable=mutations.durable)[0]
        assert not result.ok
        assert ("timeout_ms" if enabled else "recorded control flow") in result.model_text
        assert result.canonical["child_effect_ids"] == []
        assert len(mutations.durable.effects("r")) == 1
    finally:
        mutations.durable.close()


@pytest.mark.parametrize("enabled", [False, True])
def test_binding_precondition_feedback_precedes_answer_format(tmp_path: Path, enabled: bool) -> None:
    schema = structured.object_schema({"explanation": {"type": "string"}, "body": {"type": "string"}},
                                      ["explanation", "body"])
    binding = require_tool_success("browser_read", via="codemode", answer_pointer="/body")
    s = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=(
        declaration("json_explanation", json.dumps(schema)), binding)))
    read = ToolResult(ToolCall("browser_read", {}), "display", ok=True,
                      canonical={"research_observation": {"text": "actual"}})
    s.executed["read"] = record(read)
    s._memory_results["read"] = read
    with structured.prototype(tmp_path, "upload_bound", enabled=enabled):
        output = kernel_output(s)
        checks = completion_gate._required_tool_checks(s, output.text, CompletionContext(), output.observations)
        verdict = completion_gate._required_checks_verdict(s, checks.rows, checks.evaluations)
    assert verdict is not None and not verdict.complete
    assert ("tool_parent_missing" if enabled else "output_format_invalid") in verdict.followup


def kernel_output(s: TaskSession) -> completion_output.CompletionOutput:
    from codey.operations import kernel_control

    return kernel_control.prepare_completion_output(s, "model explanation", CompletionContext())


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("assembled", [False, True])
def test_upload_receipts_are_delivered_and_same_step_is_not_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, assembled: bool,
) -> None:
    attachment = InputAttachmentStore(tmp_path / "inputs", "upload-test").put(
        "fixture.txt", base64.b64encode(b"UPLOAD_BYTES_82\n").decode())
    upload = plan(step("nav", "browser_navigate", {"url": URL}),
        step("upload", "browser_upload", {"name": "Attachment", "attachment_id": str(attachment["id"])}),
        step("wait", "browser_wait", {"condition": "text", "value": "UPLOAD_BYTES_82"}),
        step("read", "browser_read", {}))
    install_http(monkeypatch, [upload, upload, {"tool": "done", "args": {"summary": "已选择附件；尚未核验服务器接收。"}}])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "upload_bound" if assembled else "upload", "B",
                        Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10), max_turns=4, structured=True)
    assert row["passed"] and row["model_calls"] == 3, row
    assert row["structured_plans"] == 2 and row["structured_reused_steps"] == 2
    assert row["successful_uploads"] == 1
    assert "UPLOAD_BYTES_82" in str(row["answer"])
    receipts = tool_receipts(directory / "state")
    assert all("value" not in r.result.canonical for r in receipts if r.result.call.name == "browser_read")
    if not assembled:
        assert '"effect_verified": false' in str(row["answer"])


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_real_stale_edit_reloads_and_preserves_external_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    initial = json.dumps({"amount": 173, "code": "QUARTZ_851"})
    fresh = json.dumps({"amount": 173, "code": "QUARTZ_851", "external_revision": EXTERNAL_FIELD})
    install_http(monkeypatch, [plan(*browser_steps(), step("file", "read_file", {"path": "result.json"}),
        step("edit", "edit", {"read_id": "file", "content": initial})),
        plan(step("file", "read_file", {"path": "result.json"})),
        plan(step("edit", "edit", {"read_id": "file", "content": fresh})),
        {"tool": "done", "args": {"summary": "已按页面实测值更新文件，并保留外部字段。"}}])
    row = probe.run_arm(tmp_path / "run", "stale_edit", "B",
        Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10), max_turns=5, structured=True)
    assert row["passed"] and row["true_stale_conflicts"] == 1 and row["fresh_read_recovery"], row


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_static_content_after_same_plan_reread_can_drop_external_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterize the live failure: a fresh credential cannot protect old content."""
    content = json.dumps({"amount": 173, "code": "QUARTZ_851"})
    install_http(monkeypatch, [
        plan(*browser_steps(), step("file", "read_file", {"path": "result.json"}),
             step("edit", "edit", {"read_id": "file", "content": content})),
        plan(step("fresh", "read_file", {"path": "result.json"}),
             step("retry", "edit", {"read_id": "fresh", "content": content})),
        {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851；已更新文件。"}},
    ])
    row = probe.run_arm(tmp_path / "run", "stale_edit", "B",
                        Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                        max_turns=4, structured=True)
    assert row["true_stale_conflicts"] == 1 and row["fresh_read_recovery"], row
    assert row["project_result"] == {"amount": 173, "code": "QUARTZ_851"}
    assert row["passed"] is False, row  # The required external_revision field was deleted.


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("scenario", ["legacy_structured", "stale_ref", "snapshot_restore", "source_failure"])
def test_structured_entry_keeps_browser_and_source_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    if scenario == "source_failure":
        steps = [step("source", "open_url", {"url": URL})]
    else:
        steps = browser_steps()
        if scenario == "legacy_structured":
            steps += [step("property", "browser_property", {"name": "__contractToken"}),
                      step("tabs", "browser_tab_list", {})]
        if scenario == "stale_ref":
            steps.insert(1, step("before", "browser_snapshot", {}))
            steps.insert(3, step("stale", "browser_click", {"name": "Load record", "snapshot_id": "before"}, allow_failure=True))
    install_http(monkeypatch, [plan(*steps), {"tool": "done", "args": {"summary":
        "来源返回 HTTP 403，无法读取正文。" if scenario == "source_failure" else "已取得实际记录：净重 173 克，校验码 QUARTZ_851。"}}])
    row = probe.run_arm(tmp_path / "run", scenario, "B",
        Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10), max_turns=4, structured=True)
    assert row["passed"] and row["structured_plans"] == 1, row
    if scenario == "legacy_structured":
        assert "SCHEMA_TOKEN_74" in str(row["answer"])
    elif scenario == "source_failure":
        assert row["stop_reason"] == "blocked" and row["model_calls"] == 2


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_invalid_late_step_prevents_earlier_navigation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_http(monkeypatch, [plan(step("nav", "browser_navigate", {"url": URL}),
                                  step("bad", "browser_wait", {"value": "ready"})),
        plan(*browser_steps()), {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}])
    row = probe.run_arm(tmp_path / "run", "basic", "B",
        Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10), max_turns=4, structured=True)
    assert row["passed"] and row["structured_rejections"] == 1, row
    navigations = row["navigations"]
    assert isinstance(navigations, list) and len(navigations) == 1
    first = decode_json_object(row["first_tool_rejection"])
    assert "Structured plan rejected before all effects" in str(first["model_text"])
    assert decode_json_object(first["audit"])["child_calls"] == 0
