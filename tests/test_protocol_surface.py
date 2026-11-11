"""Deterministic protocol ablations; all model HTTP is scripted, Chromium is real."""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import secrets
from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession, BrowserToolOutput
from codey.codemode import catalog
from codey.codemode.host import CodeModeHost
from codey.codemode.kernel import build_execute_turn_executor
from codey.codemode.runtime import NodeWorkerRuntime
from codey.codemode.types import ToolCatalogEntry
from codey.operations import completion_gate, kernel_control
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import declaration
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import ToolSuccessRequirement, require_tool_success
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.input_attachments import InputAttachmentStore
from codey.toolchain import tool_prompt
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_mechanical_surface import read_result, record
from tests.test_receipt_bound_edit_surface_probe import install_http
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime
from tests.test_structured_surface import plan, session, step
from tools import protocol_surface as surface
from tools import protocol_surface_ab as probe
from tools import structured_surface
from tools.browser_production_live_gate import DOWNLOAD_BYTES, URL
from tools.completion_artifact_audit import EffectReceipt, artifacts, tool_receipts
from tools.completion_explanation_ab import Target

TARGET = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10)


def schemas(*, defaults: bool = False, stale_refs: bool = False) -> dict[str, JsonObject]:
    return surface.entry_schemas(build_turn_snapshot(session()), defaults=defaults, stale_refs=stale_refs)


@pytest.mark.parametrize("config", ["A", "B1", "B2", "B3", "B4", surface.RECEIPT_DELIVERY, "combined"])
def test_configuration_installs_only_declared_mechanisms(tmp_path: Path, config: str) -> None:
    original = (CodeModeHost.invoke, ToolSuccessRequirement._observe_value, build_turn_snapshot,
                tool_prompt.tool_repair_text, completion_gate._required_checks_verdict)
    with surface.prototype(tmp_path, "basic", config):
        from codey.operations import kernel_protocol

        flags = surface.CONFIGS[config]
        assert (CodeModeHost.invoke is not original[0]) == flags.admission
        assert (ToolSuccessRequirement._observe_value is not original[1]) == flags.binding
        assert (kernel_protocol.build_turn_snapshot is not original[2]) == flags.structured
        assert (tool_prompt.tool_repair_text is not original[3]) == flags.structured
        assert (completion_gate._required_checks_verdict is not original[4]) == flags.binding
    assert CodeModeHost.invoke is original[0] and ToolSuccessRequirement._observe_value is original[1]


@pytest.mark.parametrize("timeout", [None, True, "5000", -1])
def test_b1_invalid_explicit_wait_does_not_execute(tmp_path: Path, timeout: object) -> None:
    spec = next(s for s in build_turn_snapshot(session()).frozen_specs if s.name == "browser_wait")
    entry = catalog.build_catalog_from_specs([spec]).entries[0]
    calls: list[ToolCall] = []

    def execute(call: ToolCall) -> ToolResult:
        calls.append(call)
        return ToolResult(call, "unreachable")

    host = CodeModeHost(execution_id="test", catalog=[entry], execute=execute)
    with surface.prototype(tmp_path, "basic", "B1"), pytest.raises(ValueError, match="timeout_ms"):
        host.invoke("browser_wait", {"condition": "text", "value": "ready", "timeout_ms": timeout})
    assert not calls


def test_b1_default_enters_original_executor_and_keeps_explicit_value(tmp_path: Path) -> None:
    spec = next(s for s in build_turn_snapshot(session()).frozen_specs if s.name == "browser_wait")
    entry = catalog.build_catalog_from_specs([spec]).entries[0]
    calls: list[dict[str, object]] = []

    def execute(call: ToolCall) -> ToolResult:
        calls.append(dict(call.args))
        return ToolResult(call, "matched", ok=True, canonical={"value": {
            "action": "wait", "condition": "text", "matched": True, "page_url": URL, "text": "ready"}})

    host = CodeModeHost(execution_id="test", catalog=[entry], execute=execute)
    with surface.prototype(tmp_path, "basic", "B1"):
        host.invoke("browser_wait", {"condition": "text", "value": "ready"})
        host.invoke("browser_wait", {"condition": "text", "value": "ready", "timeout_ms": 1234})
        rendered = catalog.render_declarations(catalog.build_catalog_snapshot([entry]))
    assert [call["timeout_ms"] for call in calls] == [5000, 1234]
    assert '"timeout_ms"?:' in rendered and "defaults to 5000" in rendered


@pytest.mark.parametrize("config", ["A", "B1"])
def test_b1_durable_invalid_parameter_reports_root_cause_without_child_intent(tmp_path: Path, config: str) -> None:
    project, state, logdir = _dirs(tmp_path)
    _, mutations, managed, revisions, _deps, sink = _open_runtime(logdir, state, "s", "r", project)
    s = TaskSession(TaskPolicy(frozenset({"control", "project.read", "browser.observe"})), project=str(project))
    from codey.operations.recovery import record_entry_policy

    record_entry_policy(mutations, session_id="s", run_id="r", policy=s.policy, durable=mutations.durable)
    try:
        with surface.prototype(tmp_path, "basic", config):
            snapshot = build_turn_snapshot(s)
            executor = build_execute_turn_executor(session=s, snapshot=snapshot, run_id="r", effect_scope="probe",
                turn=0, project_path=project, managed_outputs=managed, workspace_revision_store=revisions,
                intent_sink=sink, durable=mutations.durable, session_id="s")
            result = execute_turn(s, [ToolCall("codemode", {"code":
                "return unwrap(await tools.browser_wait({condition:'text',value:'ready',timeout_ms:null}));"}, call_id="p")],
                executors={"codemode": executor}, run_id="r", effect_scope="probe", turn=0, project_path=project,
                snapshot=snapshot, intent_sink=sink, durable=mutations.durable)[0]
        assert not result.ok
        assert ("timeout_ms" if config == "B1" else "recorded control flow") in result.model_text
        assert result.canonical["child_effect_ids"] == []
        assert len(mutations.durable.effects("r")) == 1
    finally:
        mutations.durable.close()


@pytest.mark.parametrize("defect", ["none", "parent", "receipt", "identity", "arguments", "truncated", "source_truncated", "conflict"])
def test_b2_binding_uses_original_body_and_rejects_invalid_evidence(tmp_path: Path, defect: str) -> None:
    result = read_result("complete original")
    if defect == "truncated":
        result = replace(result, truncated=True)
    if defect == "source_truncated":
        result = replace(result, canonical={"research_observation": {"text": "partial", "source": {"truncated": True}}})
    parent = ToolResult(ToolCall("codemode", {}), "parent", ok=defect != "parent")
    rows = {"read": record(result), "parent": record(parent, ["read"])}
    results = {} if defect == "receipt" else {"read": result}
    if defect == "identity":
        rows["read"]["call_id"] = "wrong"
    if defect == "arguments":
        rows["read"]["argument_digests"] = {"offset": "wrong"}
    if defect == "conflict":
        other = read_result("different")
        results["other"] = other
        rows["other"] = record(other)
        rows["parent"]["child_effect_ids"] = ["read", "other"]
    requirement = ToolSuccessRequirement("browser_read", via="codemode", answer_pointer="/body")
    with surface.prototype(tmp_path, "upload_bound", "B2"):
        outcome = requirement.observe(rows, results=results)
        assert "research_observation.text" in requirement.model_guidance(explanation=True)
    assert outcome.evaluation.passed == (defect == "none")
    if defect == "none":
        assert outcome.value == "complete original" and outcome.result_refs == ("read",)
    assert "value" not in result.canonical


@pytest.mark.parametrize("config", ["A", "B2"])
def test_b2_feedback_identifies_missing_parent_before_answer_format(tmp_path: Path, config: str) -> None:
    schema = structured_surface.object_schema({"explanation": {"type": "string"}, "body": {"type": "string"}},
                                             ["explanation", "body"])
    s = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=(declaration("json_explanation", json.dumps(schema)),
        require_tool_success("browser_read", via="codemode", answer_pointer="/body"))))
    result = read_result("real")
    s.executed["read"] = record(result)
    s._memory_results["read"] = result
    with surface.prototype(tmp_path, "upload_bound", config):
        output = kernel_control.prepare_completion_output(s, "explanation", CompletionContext())
        checks = completion_gate._required_tool_checks(s, output.text, CompletionContext(), output.observations)
        verdict = completion_gate._required_checks_verdict(s, checks.rows, checks.evaluations)
    assert verdict is not None and not verdict.complete
    assert "tool_parent_missing" in verdict.followup


@pytest.mark.parametrize(("state", "reason"), [
    ("missing_action", "tool_execution_missing"),
    ("failed_parent", "tool_parent_missing"),
    ("bad_source", "observation_receipt_invalid"),
    ("truncated", "observation_selector_unavailable"),
])
def test_receipt_delivery_explains_missing_evidence_before_format(tmp_path: Path, state: str, reason: str) -> None:
    schema = structured_surface.object_schema({"explanation": {"type": "string"}, "body": {"type": "string"}},
                                             ["explanation", "body"])
    task = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=(declaration("json_explanation", json.dumps(schema)),
        require_tool_success("browser_read", via="codemode", answer_pointer="/body"))))
    read = replace(read_result("unverified"), truncated=state == "truncated")
    parent = ToolResult(ToolCall("codemode", {}), "parent", ok=state != "failed_parent")
    if state != "missing_action":
        task.executed.update(read=record(read), parent=record(parent, ["read"]))
        task._memory_results["read"] = read
    with surface.prototype(tmp_path, "upload_bound", surface.RECEIPT_DELIVERY):
        output = kernel_control.prepare_completion_output(task, "explanation", CompletionContext())
        checks = completion_gate._required_tool_checks(task, output.text, CompletionContext(), output.observations)
        verdict = completion_gate._required_checks_verdict(task, checks.rows, checks.evaluations)
    assert verdict is not None and not verdict.complete
    assert reason in verdict.followup and "output_format_invalid" not in verdict.followup


def test_b3_frozen_permissions_descriptions_and_no_implicit_b1_defaults() -> None:
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control", "project.read"}))))
    limited = surface.entry_schemas(snapshot, defaults=False, stale_refs=False)
    assert not {"edit", "browser_property", "browser_upload"} & limited.keys()
    entries = schemas()
    assert "timeout_ms" in entries["browser_wait"]["required"]
    assert "timeout_ms" not in schemas(defaults=True)["browser_wait"]["required"]
    assert "Result data schema" in str(entries["browser_property"]["description"])
    assert "target_name" in entries["browser_upload"]["required"]
    assert "snapshot_id" not in decode_json_object(entries["browser_click"]["properties"])


@pytest.mark.parametrize("defect", ["same_plan", "invented", "reread_old_handle", "missing_timeout", "illegal_property"])
def test_b3_invalid_plan_has_no_effect(defect: str) -> None:
    reads: JsonObject = {"receipt-1": {"path": "result.json", "result_ref": "receipt-1"}}
    steps = [step("read", "read_file", {"path": "result.json"})]
    if defect in {"same_plan", "invented", "reread_old_handle"}:
        handle = "receipt-1" if defect == "reread_old_handle" else "read" if defect == "same_plan" else "invented"
        steps.append(step("edit", "edit", {"read_handle": handle, "content": "{}"}))
    elif defect == "missing_timeout":
        steps.append(step("wait", "browser_wait", {"condition": "text", "value": "ready"}))
    else:
        steps.append(step("property", "browser_property", {"name": "x;bad()"}))
    with pytest.raises(ValueError):
        surface.compile_plan({"steps": steps}, schemas(), reads, defaults=False)


@pytest.mark.parametrize("defect", ["none", "failed_parent", "missing_parent", "partial", "large", "truncated",
                                    "tail_only", "has_next_page"])
def test_b3_only_complete_prior_parent_reads_are_delivered(defect: str) -> None:
    data = {"path": "result.json", "text_complete": defect != "partial", "text": "x" * (12001 if defect == "large" else 5),
            "start_line": 2 if defect == "tail_only" else 1, "end_line": 2,
            "total_lines": 3 if defect == "has_next_page" else 2, "next_offset": 3 if defect == "has_next_page" else 0}
    read = ToolResult(ToolCall("read_file", {"path": "result.json"}), "file", ok=True,
                      canonical={"value": data}, truncated=defect == "truncated")
    parent = ToolResult(ToolCall("codemode", {}), "parent", ok=defect != "failed_parent",
                        canonical={"child_effect_ids": ["original-receipt"]})
    results = {"original-receipt": read}
    if defect != "missing_parent":
        results["parent"] = parent
    reads, observations = surface.delivered_reads(results)
    assert bool(reads) == (defect == "none")
    if reads:
        assert observations["original-receipt"] == {"path": "result.json", "text": "xxxxx", "text_complete": True}
        assert reads["original-receipt"] == {"path": "result.json", "result_ref": "original-receipt"}


@pytest.mark.parametrize("defect", ["none", "omitted", "partial", "different"])
def test_b3_context_reduction_revokes_undelivered_edit_handles(defect: str) -> None:
    reads: JsonObject = {"read": {"path": "result.json", "result_ref": "read"}}
    observations: JsonObject = {"read": {"text": "complete file", "path": "result.json", "text_complete": True}}
    actual = observations if defect == "none" else {} if defect == "omitted" else {
        "read": {"text": "complete" if defect == "partial" else "other file", "path": "result.json", "text_complete": True}}
    payload: JsonObject = {"messages": [{"role": "user", "content": surface.READ_OBSERVATIONS_PREFIX + json.dumps(actual)}]}
    assert surface.transmitted_reads(payload, reads, observations) == (reads if defect == "none" else {})


@pytest.mark.parametrize("matches", [0, 2])
def test_b3_target_error_returns_real_candidates_without_upload(matches: int) -> None:
    calls: list[str] = []

    def invoke(name: str, args: dict[str, object]) -> object:
        calls.append(name)
        return {"ok": True, "value": {"page_id": "page", "generation": 4,
            "elements": [{"name": "Attachment", "role": "button", "ref": str(i)} for i in range(matches)]}}

    compiled = surface.compile_plan({"steps": [step("upload", "browser_upload", {
        "target_name": "Attachment", "attachment_id": "att_" + "0" * 64})]}, schemas(), {}, defaults=False)
    result = NodeWorkerRuntime().execute(str(compiled["code"]), inputs=decode_json_object(compiled["inputs"]),
        catalog=[ToolCatalogEntry(name, name) for name in ("browser_snapshot", "browser_upload")], invoke=invoke)
    assert result.ok and ("target_ambiguous" if matches else "target_missing") in str(result.value)
    assert calls == ["browser_snapshot"]


def test_b3_old_handle_never_acquires_a_new_version() -> None:
    captured: list[dict[str, object]] = []

    def invoke(name: str, args: dict[str, object]) -> object:
        assert name == "edit"
        captured.append(args)
        return {"ok": False, "error": "file version changed"}

    compiled = surface.compile_plan({"steps": [step("write", "edit", {"read_handle": "old", "content": "{}"})]},
        schemas(), {"old": {"path": "result.json", "result_ref": "original-old-receipt"}}, defaults=False)
    result = NodeWorkerRuntime().execute(str(compiled["code"]), inputs=decode_json_object(compiled["inputs"]),
        catalog=[ToolCatalogEntry("edit", "Edit")], invoke=invoke)
    assert result.ok and "file version changed" in str(result.value)
    assert captured == [{"path": "result.json", "base_result_ref": "original-old-receipt", "content": "{}"}]


def browser_steps() -> list[JsonObject]:
    return [step("nav", "browser_navigate", {"url": URL}),
        step("fill", "browser_fill", {"target_name": "Locality", "value": "福州"}),
        step("click", "browser_click", {"target_name": "Load record"}),
        step("wait", "browser_wait", {"condition": "text", "value": "鱼丸", "timeout_ms": 5000}),
        step("body", "browser_read", {})]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("arm", ["A", "B"])
def test_b2_real_upload_binding_paired_without_edit_or_typed_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str) -> None:
    attachment = InputAttachmentStore(tmp_path / "inputs", "upload").put("fixture.txt", base64.b64encode(b"UPLOAD_BYTES_82\n").decode())
    code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
        "const s=unwrap(await tools.browser_snapshot({}));"
        "unwrap(await tools.browser_upload({ref:s.elements.find(e=>e.name==='Attachment').ref,attachment_id:inputs.attachment}));"
        "unwrap(await tools.browser_wait({condition:'text',value:'UPLOAD_BYTES_82',timeout_ms:5000}));"
        "return unwrap(await tools.browser_read({}));")
    done: JsonObject = {"tool": "done", "args": {"summary": "已选择附件，服务器接收未核验。"}}
    install_http(monkeypatch, [{"tool": "codemode", "args": {"code": code, "inputs": {"url": URL, "attachment": attachment["id"]}}},
                               done, done, done])
    row = probe.run_arm(tmp_path / "run", "upload_bound", arm, "B2", TARGET, max_turns=4)
    assert row["passed"], row
    assert decode_json_object(row["metrics"])["browser_upload"] == 1
    if arm == "B":
        assert "UPLOAD_BYTES_82" in str(row["answer"])


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("scenario,arm", [
    ("upload_bound", "B"), ("upload", "A"), ("upload", "B"),
    ("download", "A"), ("download", "B"), ("return_failure", "B"),
])
def test_receipt_delivery_uses_original_file_and_page_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, arm: str,
) -> None:
    upload = scenario.startswith("upload")
    filename = "fixture.txt" if upload else "fixture-download.txt"
    data = b"UPLOAD_BYTES_82\n" if upload else DOWNLOAD_BYTES
    inputs: JsonObject = {"url": URL}
    code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
            "const s=unwrap(await tools.browser_snapshot({}));")
    if upload:
        attachment = InputAttachmentStore(tmp_path / "inputs", "upload").put(filename, base64.b64encode(data).decode())
        inputs["attachment"] = attachment["id"]
        code += ("unwrap(await tools.browser_upload({ref:s.elements.find(e=>e.name==='Attachment').ref,"
                 "attachment_id:inputs.attachment}));"
                 "unwrap(await tools.browser_wait({condition:'text',value:'UPLOAD_BYTES_82',timeout_ms:5000}));")
    else:
        code += "unwrap(await tools.browser_download({ref:s.elements.find(e=>e.name==='Download fixture').ref}));"
    code += "return unwrap(await tools.browser_read({}));"
    summary = "已完成工具操作，请查看实际回执。"
    done: JsonObject = {"tool": "done", "args": {"summary": summary}}
    install_http(monkeypatch, [done] if scenario == "return_failure" else [
        {"tool": "codemode", "args": {"code": code, "inputs": inputs}}, done])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, scenario, arm, surface.RECEIPT_DELIVERY, TARGET, max_turns=4)
    assert row["stop_reason"] == "done" and row["passed"] is (arm == "B"), row
    tool = "browser_upload" if upload else "browser_download"
    metrics = decode_json_object(row["metrics"])
    assert metrics[tool] == 1
    assert metrics["file_deliveries"] == (1 if arm == "B" and scenario != "upload_bound" else 0)
    if arm == "A":
        assert row["answer"] == summary
        return
    receipts = tool_receipts(directory / "state")
    effect = next(r for r in receipts if r.result.call.name == tool)
    value = decode_json_object(effect.result.canonical["value"])
    answer = str(row["answer"])
    facts = json.loads(answer if scenario == "upload_bound" else answer.split("```json\n")[1].split("\n```")[0])
    assert facts["filename"] == filename and facts["sha256"] == value["sha256"] == hashlib.sha256(data).hexdigest()
    if upload:
        read = next(r.result for r in receipts if r.result.call.name == "browser_read")
        assert surface.complete_browser_read(read)
        incomplete = replace(read, canonical={**read.canonical, "read_passages": [{"start": 1, "end": 2}]})
        assert not surface.complete_browser_read(incomplete)
        assert facts["page_text"] == decode_json_object(read.canonical["research_observation"])["text"]
        assert value["effect_verified"] is False
        if scenario == "upload":
            assert facts["effect_verified"] is False and surface.UPLOAD_SCOPE_NOTE in answer
        else:
            assert facts["explanation"] == summary + "\n\n" + surface.UPLOAD_SCOPE_NOTE
    else:
        assert facts["bytes"] == len(data) and facts["path"] == value["path"]
    if scenario == "return_failure":
        assert row["model_calls"] == 1 and metrics["automatic_receipt_recoveries"] == 1 and metrics["repaired_parent"]
        requests = [event["payload"] for event in map(json.loads, (directory / "provider.jsonl").read_text(
            encoding="utf-8").splitlines()) if event.get("phase") == "request"]
        results = [json.loads(content.split("Result data (JSON):\n", 1)[1].split("\n\nResult metadata:", 1)[0])
                   for message in requests[0]["messages"] if (content := message.get("content", "")).startswith(
                       "[result: codemode]\nResult data (JSON):")]
        replayed = next(item["value"]["replayed"] for item in results if item["value"].get("replayed"))
        download = next(item for item in replayed if item["tool"] == tool)
        read = next(r for r in receipts if r.result.call.name == "browser_read")
        body = next(item for item in replayed if item["tool"] == "browser_read")
        assert download["receipt_ref"] == effect.identity and download["data"] == value and not download["truncated"]
        assert body["receipt_ref"] == read.identity and body["data"]["text"] == read.result.model_text and not body["truncated"]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_b3_real_property_target_and_consistent_repair_wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    actions = browser_steps() + [step("token", "browser_property", {"name": "__contractToken"}),
                                 step("tabs", "browser_tab_list", {})]
    bad = plan(step("bad", "browser_property", {"name": "invalid;code"}))
    install_http(monkeypatch, [bad, plan(*actions), {"tool": "done", "args": {
        "summary": "净重 173 克，校验码 QUARTZ_851。token SCHEMA_TOKEN_74，URL " + URL}}])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "legacy_structured", "B", "B3", TARGET, max_turns=4)
    assert row["passed"], row
    requests = [json.loads(line) for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("phase") == "request"]
    assert len(requests) == 3
    wire = json.dumps(requests[1], ensure_ascii=False)
    assert "Plan rejected before effects" in wire and "Execute ordered JSON steps" in wire
    assert "Write an async JavaScript function body" not in wire and "return unwrap(await tools.browser_read" not in wire
    assert "call tools.browser_eval" not in wire
    assert "op=browser_property" in wire


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_b3_real_conflict_observation_boundary_and_external_field_preservation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "run"
    exchange = 0
    import tools.completion_explanation_ab as provider_module

    external = {"revision": 2, "marker": secrets.token_hex(16)}
    monkeypatch.setattr(probe.gate, "EXTERNAL_FIELD", external)

    def scripted(url: str, payload: dict[str, object], headers: dict[str, str], **kwargs: object) -> dict[str, object]:
        nonlocal exchange
        exchange += 1
        messages = payload["messages"]
        assert isinstance(messages, list)
        read_message = next(str(m["content"]) for m in reversed(messages) if isinstance(m, dict)
                            and str(m.get("content", "")).startswith("Complete file observations"))
        observations = json.loads(read_message.split("\n", 1)[1])
        if exchange == 1:
            action = plan(*browser_steps(), step("file", "read_file", {"path": "result.json"}))
        elif exchange in {2, 4}:
            handle, observed = next(iter(observations.items()))
            content = json.loads(observed["text"])
            content.update(amount=173, code="QUARTZ_851")
            action = plan(step("write", "edit", {"read_handle": handle, "content": json.dumps(content)}))
        elif exchange == 3:
            action = plan(step("fresh", "read_file", {"path": "result.json"}))
        else:
            action = {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851，已更新并保留其他字段。"}}
        observe = kwargs["observe"]
        assert callable(observe)
        observe(attempt=1, data=json.dumps(payload).encode(), phase="request", response_bytes=0, seconds=0.0)
        return surface.script_body(action)

    monkeypatch.setattr("codey.providers.api_generation.generate", scripted)
    monkeypatch.setattr(provider_module, "service_state", lambda _url: {})
    row = probe.run_arm(directory, "stale_edit", "B", "B3", TARGET, max_turns=6)
    assert row["passed"] and row["true_stale_conflicts"] == 1, row
    assert decode_json_object(row["project_result"])["external_revision"] == external
    assert not row["external_field_lost"]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("arm", ["A", "B"])
def test_edit_boundary_rejects_new_receipt_for_precomposed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str,
) -> None:
    external = {"marker": secrets.token_hex(16)}
    monkeypatch.setattr(probe.gate, "EXTERNAL_FIELD", external)
    exchange = 0
    directory = tmp_path / "run"
    original_text = ""
    read_step = step("file", "read_file", {"path": "result.json"})
    done: JsonObject = {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}

    def scripted(url: str, payload: dict[str, object], headers: dict[str, str], **kwargs: object) -> dict[str, object]:
        nonlocal exchange, original_text
        exchange += 1
        messages = payload["messages"]
        assert isinstance(messages, list)
        message = next(m["content"] for m in reversed(messages) if isinstance(m, dict)
                       and str(m.get("content", "")).startswith(surface.READ_OBSERVATIONS_PREFIX))
        observed = json.loads(message[len(surface.READ_OBSERVATIONS_PREFIX):])
        if exchange == 1:
            assert not observed
            action = plan(*browser_steps(), read_step)
        elif exchange == 2:
            assert len(observed) == 1
            old_ref, old_file = next(iter(observed.items()))
            original = json.loads(old_file["text"])
            assert "external_revision" not in original
            original.update(amount=173, code="QUARTZ_851")
            original_text = json.dumps(original)
            action = plan(step("old_write", "edit", {"read_handle": old_ref, "content": original_text}))
        elif exchange == 3:
            action = plan(read_step, step("unsafe_retry", "edit", {"read_handle": "file", "content": original_text}))
        elif arm == "A":
            action = done
        elif exchange == 4:
            assert "read_not_delivered" in json.dumps(messages)
            assert all("external_revision" not in file["text"] for file in observed.values())
            action = plan(read_step)
        elif exchange == 5:
            fresh_ref, fresh = next((ref, json.loads(file["text"])) for ref, file in observed.items()
                                    if "external_revision" in file["text"])
            assert fresh["external_revision"] == external
            fresh.update(amount=173, code="QUARTZ_851")
            action = plan(step("fresh_write", "edit", {"read_handle": fresh_ref, "content": json.dumps(fresh)}))
        else:
            action = done
        observe = kwargs["observe"]
        assert callable(observe)
        observe(attempt=1, data=json.dumps(payload).encode(), phase="request", response_bytes=0, seconds=0.0)
        return surface.script_body(action)

    monkeypatch.setattr("codey.providers.api_generation.generate", scripted)
    monkeypatch.setattr("tools.completion_explanation_ab.service_state", lambda _url: {})
    row = probe.run_arm(directory, "stale_edit", arm, surface.EDIT_BOUNDARY, TARGET, max_turns=7)
    assert row["true_stale_conflicts"] == 1 and row["fresh_read_recovery"], row
    assert row["passed"] is (arm == "B") and row["external_field_lost"] is (arm == "A"), row
    assert decode_json_object(row["metrics"])["static_rejections"] == int(arm == "B")
    receipts = tool_receipts(directory / "state")
    reads = [r for r in receipts if r.result.ok and r.result.call.name == "read_file"]
    writes = [r for r in receipts if r.result.call.name == "edit"]
    assert len(reads) == len(writes) == 2
    assert not writes[0].result.ok and writes[1].result.ok
    assert writes[1].result.call.args["base_result_ref"] == reads[1].identity
    if arm == "B":
        assert decode_json_object(row["project_result"])["external_revision"] == external
    else:
        assert writes[1].result.call.args["content"] == original_text


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_codemode_can_compute_whole_file_after_reading_in_same_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = decode_json_object(page_reset_action()["args"])["code"]
    assert isinstance(browser, str)
    code = browser.replace("return unwrap(await tools.browser_read({}));",
        "const body=unwrap(await tools.browser_read({}));"
        "const read=await tools.read_file({path:'result.json'});"
        "const current=JSON.parse(unwrap(read).text);"
        r"current.amount=Number(body.match(/净重为\s+(\d+)/)[1]); current.code=body.match(/校验码\s+(\w+)/)[1];"
        "return await tools.edit({path:'result.json',base_result_ref:read.result_ref,content:JSON.stringify(current)});")
    action: JsonObject = {"tool": "codemode", "args": {"code": code, "inputs": {"url": URL}}}
    install_http(monkeypatch, [action, action, {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}])
    row = probe.run_arm(tmp_path / "run", "stale_edit", "A", "B3", TARGET, max_turns=4)
    assert row["passed"] and row["true_stale_conflicts"] == 1 and row["fresh_read_recovery"], row
    assert not row["external_field_lost"]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_b3_real_upload_uses_control_name_and_preserves_raw_result_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attachment = InputAttachmentStore(tmp_path / "inputs", "upload").put("fixture.txt", base64.b64encode(b"UPLOAD_BYTES_82\n").decode())
    action = plan(step("nav", "browser_navigate", {"url": URL}),
        step("upload", "browser_upload", {"target_name": "Attachment", "attachment_id": attachment["id"]}),
        step("wait", "browser_wait", {"condition": "text", "value": "UPLOAD_BYTES_82", "timeout_ms": 5000}),
        step("body", "browser_read", {}))
    install_http(monkeypatch, [action, {"tool": "done", "args": {"summary":
        "fixture.txt UPLOAD_BYTES_82 SHA256 " + hashlib.sha256(b"UPLOAD_BYTES_82\n").hexdigest()}}])
    row = probe.run_arm(tmp_path / "run", "upload", "B", "B3", TARGET, max_turns=3)
    assert row["passed"], row
    assert decode_json_object(row["metrics"])["browser_upload"] == 1


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_b1_real_wait_omission_uses_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
        "let s=unwrap(await tools.browser_snapshot({}));"
        "unwrap(await tools.browser_fill({ref:s.elements.find(e=>e.name==='Locality').ref,value:'福州'}));"
        "s=unwrap(await tools.browser_snapshot({}));"
        "unwrap(await tools.browser_click({ref:s.elements.find(e=>e.name==='Load record').ref}));"
        "unwrap(await tools.browser_wait({condition:'text',value:'鱼丸'}));"
        "return unwrap(await tools.browser_read({}));")
    install_http(monkeypatch, [{"tool": "codemode", "args": {"code": code, "inputs": {"url": URL}}},
        {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "basic", "B", "B1", TARGET, max_turns=3)
    assert row["passed"], row
    from tools.completion_artifact_audit import tool_receipts

    waits = [r.result for r in tool_receipts(directory / "state") if r.result.call.name == "browser_wait"]
    assert len(waits) == 1 and waits[0].call.args["timeout_ms"] == 5000


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("arm,config", [("A", "B4"), ("B", "B4"), ("B", "combined")])
def test_b4_real_return_failure_repairs_without_second_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                               arm: str, config: str) -> None:
    done: JsonObject = {"tool": "done", "args": {"summary":
        "fixture-download.txt SHA256 " + hashlib.sha256(DOWNLOAD_BYTES).hexdigest()}}
    install_http(monkeypatch, [done, done, done])
    row = probe.run_arm(tmp_path / "run", "return_failure", arm, config, TARGET, max_turns=4)
    assert row["passed"], row
    metrics = decode_json_object(row["metrics"])
    assert metrics["browser_download"] == 1
    assert metrics["mechanical_repairs"] == 0 and metrics["automatic_receipt_recoveries"] == 1
    assert row["model_calls"] == 1
    assert row["acquisition"] == "scripted-fault-then-model"
    requests = [json.loads(line) for line in (tmp_path / "run/provider.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("phase") == "request"]
    assert requests and all(event["payload"]["stream"] is True for event in requests)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize(("suffix", "failure"), [
    ("", "missing_return"), ("return {broken:undefined};", "result_encoding"),
    ("return download.path;", "result_contract"),
])
def test_production_download_delivers_only_authorized_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, failure: str,
) -> None:
    code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
            "const s=unwrap(await tools.browser_snapshot({}));"
            "const download=await tools.browser_download({ref:s.elements.find(e=>e.name==='Download fixture').ref});"
            "unwrap(await tools.browser_read({}));" + suffix)
    digest = hashlib.sha256(DOWNLOAD_BYTES).hexdigest()
    install_http(monkeypatch, [
        {"tool": "codemode", "args": {"code": code, "inputs": {"url": URL}}},
        {"tool": "done", "args": {"summary": "fixture-download.txt SHA256 " + digest}},
    ])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "download", "A", "B4", TARGET, max_turns=3)
    assert row["passed"] and row["model_calls"] == 2, row
    receipts = tool_receipts(directory / "state")
    download, = [r for r in receipts if r.result.call.name == "browser_download"]
    read, = [r for r in receipts if r.result.call.name == "browser_read"]
    parent, = [r.result for r in receipts if r.result.call.name == "codemode"]
    assert download.result.ok and download.result.canonical["value"]["sha256"] == digest
    if failure == "result_contract":
        assert not parent.ok and parent.audit["error_code"] == failure
        assert "repair" not in parent.canonical and "recovered_return" not in parent.canonical
        return
    assert parent.ok and parent.audit["original_error_code"] == failure
    assert parent.canonical["recovered_return"] is True and "repair" not in parent.canonical
    assert decode_json_object(row["metrics"])["mechanical_repairs"] == 0
    replayed = parent.canonical["value"]["replayed"]
    assert [item["receipt_ref"] for item in replayed] == parent.canonical["child_effect_ids"]
    assert replayed[-2]["receipt_ref"] == download.identity
    assert replayed[-2]["data"] == download.result.canonical["value"]
    assert replayed[-1]["receipt_ref"] == read.identity
    assert replayed[-1]["data"] == {"text": read.result.model_text}
    requests = [event["payload"] for event in map(json.loads, (directory / "provider.jsonl").read_text(
        encoding="utf-8").splitlines()) if event.get("phase") == "request"]
    results = [json.loads(content.split("Result data (JSON):\n", 1)[1].split("\n\nResult metadata:", 1)[0])
               for message in requests[1]["messages"] if (content := message.get("content", "")).startswith(
                   "[result: codemode]\nResult data (JSON):")]
    assert any(item["value"]["replayed"] == replayed for item in results)


def page_reset_action(*, navigate: bool = True, reread: bool = False) -> JsonObject:
    code = "unwrap(await tools.browser_navigate({url:inputs.url}));" if navigate else ""
    code += ("let s=unwrap(await tools.browser_snapshot({}));"
        "unwrap(await tools.browser_fill({ref:s.elements.find(e=>e.name==='Locality').ref,value:'福州'}));"
        "s=unwrap(await tools.browser_snapshot({}));"
        "unwrap(await tools.browser_click({ref:s.elements.find(e=>e.name==='Load record').ref}));"
        "unwrap(await tools.browser_wait({condition:'text',value:'鱼丸',timeout_ms:5000}));")
    if reread:
        code += "unwrap(await tools.browser_read({}));"
    code += "return unwrap(await tools.browser_read({}));"
    return {"tool": "codemode", "args": {"code": code, "inputs": {"url": URL}}}


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("config", ["B4", "combined"])
def test_b4_page_reset_requires_real_second_fill_and_click(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str) -> None:
    action = plan(*browser_steps()) if config == "combined" else page_reset_action()
    install_http(monkeypatch, [action, action, {"tool": "done", "args": {"summary": "两次加载完成，净重 173，校验码 QUARTZ_851。"}}])
    row = probe.run_arm(tmp_path / "run", "page_reset", "B", config, TARGET, max_turns=4)
    assert row["passed"], row
    metrics = decode_json_object(row["metrics"])
    assert metrics["browser_fill"] == metrics["browser_click"] == 2


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("arm", ["A", "B"])
def test_page_reset_gate_rejects_early_done_and_accepts_completed_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str,
) -> None:
    action = page_reset_action()
    done: JsonObject = {"tool": "done", "args": {"summary": "两次加载完成，净重 173，校验码 QUARTZ_851。"}}
    install_http(monkeypatch, [action, done, action, done])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "page_reset", arm, probe.PAGE_RESET_GATE, TARGET, max_turns=5)
    assert row["stop_reason"] == "done", row
    assert row["passed"] is (arm == "B"), row
    assert row["silent_failure"] is (arm == "A"), row
    metrics = decode_json_object(row["metrics"])
    assert metrics["verified_rounds"] == metrics["browser_navigate"] == (2 if arm == "B" else 1)
    checks = row["page_reset_gate_checks"]
    assert isinstance(checks, list)
    if arm == "A":
        assert checks == []
        return

    early, final = map(decode_json_object, checks)
    assert early["next_missing_tool"] == "browser_navigate" and early["done_accepted"] is False
    assert final["completed_rounds"] == 2 and final["done_accepted"] is True
    requests = [json.loads(line)["payload"] for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("phase") == "request"]
    assert "第 2 轮下一步是 browser_navigate" in json.dumps(requests[2]["messages"], ensure_ascii=False)
    decisions = [body["data"] for _, body in artifacts(directory / "state")
                 if isinstance(body.get("data"), dict) and body["data"].get("purpose") == "done"]
    assert any(probe.PAGE_RESET_CHECK in json.dumps(decision) for decision in decisions)

    receipts = tool_receipts(directory / "state")
    seen, _ = probe.page_reset_progress(receipts)
    second = seen[1]
    for missing_index in (0, 2, 3, 4, 5):
        completed, _ = probe.page_reset_progress([receipt for receipt in receipts if receipt.identity != second[missing_index]])
        assert len(completed) == 1
    second_wait = next(index for index, receipt in enumerate(receipts) if receipt.identity == second[4])
    first_nav = next(receipt for receipt in receipts if receipt.identity == seen[0][0])
    reset = replace(first_nav, identity="intervening-navigation")
    completed, partial = probe.page_reset_progress([*receipts[:second_wait], reset, *receipts[second_wait:]])
    assert len(completed) == 1 and partial == [reset.identity]


def test_b4_history_does_not_claim_current_form_state() -> None:
    old = ToolResult(ToolCall("browser_upload", {}), "uploaded", ok=True, canonical={"value": {"name": "a.txt"}})
    new = ToolResult(ToolCall("browser_navigate", {}), "navigated", ok=True, canonical={"value": {"page_id": "new"}})
    state = surface.recovery_state([EffectReceipt("old", old, None), EffectReceipt("new", new, None)])
    assert decode_json_object(state["latest_page_transition"])["receipt_ref"] == "new"
    assert "not current form state" in str(state["rule"])


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("arm", ["A", "B"])
def test_page_reset_runtime_navigation_starts_once_and_still_requires_second_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str,
) -> None:
    done: JsonObject = {"tool": "done", "args": {"summary": "净重 173，校验码 QUARTZ_851。"}}
    install_http(monkeypatch, [page_reset_action(reread=True), done, page_reset_action(navigate=arm == "A"), done])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "page_reset", arm, probe.PAGE_RESET_NAVIGATION, TARGET, max_turns=7)
    assert row["passed"] and row["page_reset_gate_enabled"], row
    assert row["model_calls"] == 4
    nav = decode_json_object(row["runtime_navigation"])
    metrics = decode_json_object(row["metrics"])
    assert metrics["browser_navigate"] == metrics["verified_rounds"] == 2
    checks = row["page_reset_gate_checks"]
    assert isinstance(checks, list)
    early, final = map(decode_json_object, checks)
    assert early["done_accepted"] is False
    assert early["next_missing_tool"] == ("browser_snapshot" if arm == "B" else "browser_navigate")
    assert final["done_accepted"] is True

    receipts = tool_receipts(directory / "state")
    completed, _ = probe.page_reset_progress(receipts)
    first_read, second_nav = completed[0][-1], completed[1][0]
    read_index = next(i for i, receipt in enumerate(receipts) if receipt.identity == first_read)
    nav_index = next(i for i, receipt in enumerate(receipts) if receipt.identity == second_nav)
    assert read_index < nav_index
    assert probe.page_reset_scheduler()(receipts[:read_index]) is None
    assert probe.page_reset_scheduler()(receipts[:nav_index + 1]) is None
    schedule = probe.page_reset_scheduler()
    replay = EffectReceipt("replay", ToolResult(ToolCall("codemode", {"repair_of": "original"}), "replayed", ok=True,
                                               canonical={"child_effect_ids": [first_read]}), None)
    assert schedule([*receipts[:read_index + 1], replay]) is not None
    assert schedule(receipts[:nav_index]) is None
    if arm == "A":
        assert nav["scheduled_exchanges"] == [] and nav["navigation_ref"] is None
        return

    assert nav["scheduled_exchanges"] == [2] and nav["parent_count"] == 1
    assert nav["success"] is True and nav["first_round_read"] == first_read and nav["navigation_ref"] == second_nav
    requests = [json.loads(line) for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("phase") == "request"]
    assert requests[1]["exchange"] == 3
    messages = json.dumps(requests[1]["payload"]["messages"], ensure_ascii=False)
    assert "第二轮已导航" in messages and second_nav in messages


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_page_reset_runtime_navigation_delivers_failure_without_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    navigate = BrowserSession._navigate_on_browser_thread
    error = "ERROR: scripted fixture navigation failure"
    calls: list[str] = []

    def fail_second(session: BrowserSession, url: str) -> BrowserToolOutput:
        calls.append(url)
        if len(calls) == 2:
            return BrowserToolOutput(error, False, {"action": "navigate", "url": url, "delivery": "uncertain"})
        return navigate(session, url)

    monkeypatch.setattr(BrowserSession, "_navigate_on_browser_thread", fail_second)
    done: JsonObject = {"tool": "done", "args": {"summary": "净重 173，校验码 QUARTZ_851。"}}
    install_http(monkeypatch, [page_reset_action(), done, done, done, done])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "page_reset", "B", probe.PAGE_RESET_NAVIGATION, TARGET, max_turns=7)
    nav = decode_json_object(row["runtime_navigation"])
    assert calls == [URL, URL]
    assert row["passed"] is False and row["stop_reason"] == "no_progress"
    assert nav["scheduled_exchanges"] == [2] and nav["success"] is False and nav["result"] == error
    requests = [json.loads(line) for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("phase") == "request"]
    assert error in json.dumps(requests[1]["payload"]["messages"], ensure_ascii=False)


@pytest.mark.parametrize("defect", ["runtime", "failed_child", "missing_child", "edit", "attempted", "repaired"])
def test_b4_data_delivery_preserves_replay_refusals(defect: str) -> None:
    parent = ToolResult(ToolCall("codemode", {}), "failed", ok=False, canonical={
        "repair": {"token": "token", "failure_code": "runtime_error" if defect == "runtime" else "result_encoding",
                   "parent_effect_id": "parent"}, "child_effect_ids": ["child"]})
    child = ToolResult(ToolCall("edit" if defect == "edit" else "browser_read", {}), "body", ok=defect != "failed_child")
    receipts = [EffectReceipt("parent", parent, None)]
    if defect != "missing_child":
        receipts.append(EffectReceipt("child", child, None))
    if defect == "repaired":
        receipts.append(EffectReceipt("repaired", ToolResult(ToolCall("codemode", {}), "done", ok=True,
            canonical={"repaired_from": "parent"}), None))
    assert surface.recovery_action(receipts, {"token"} if defect == "attempted" else set()) is None


def test_b4_replay_bounds_data_and_preserves_original_truncation() -> None:
    children = [
        ToolResult(ToolCall("browser_read", {}), "x" * (surface.RECOVERY_DATA_CHARS + 1), ok=True),
        ToolResult(ToolCall("browser_read", {}), "partial source", ok=True,
                   canonical={"research_observation": {"source": {"truncated": True}}}),
        ToolResult(ToolCall("browser_download", {}), "downloaded", ok=True,
                   canonical={"value": {"filename": "actual.txt", "sha256": "original"}}),
    ]
    references = ["large", "partial", "download"]
    parent = ToolResult(ToolCall("codemode", {}), "failed", ok=False, canonical={
        "repair": {"token": "token", "failure_code": "result_encoding", "parent_effect_id": "parent"},
        "child_effect_ids": references})
    candidate = surface.recovery_action([EffectReceipt("parent", parent, None),
        *[EffectReceipt(ref, child, None) for ref, child in zip(references, children, strict=True)]], set())
    assert candidate is not None
    args = decode_json_object(candidate[1]["args"])
    replayed = iter(zip(references, children, strict=True))
    calls: list[str] = []

    def invoke(name: str, params: dict[str, object]) -> object:
        ref, child = next(replayed)
        assert name == child.call.name and params == child.call.args
        calls.append(ref)
        return {"ok": True, "result_ref": ref, "value": child.canonical.get("value", child.model_text)}

    result = NodeWorkerRuntime().execute(str(args["code"]), inputs=decode_json_object(args["inputs"]),
        catalog=[ToolCatalogEntry(name, name) for name in ("browser_read", "browser_download")], invoke=invoke)
    assert result.ok, result
    value = decode_json_object(result.value)
    assert value["receipts_replayed"] == 3 and calls == references
    rows = value["replayed"]
    assert isinstance(rows, list)
    large, partial, download = [decode_json_object(row) for row in rows]
    assert large["data"] is None and large["data_omitted"] and large["truncated"]
    assert large["omission_reason"] == "recovery_delivery_limit"
    assert partial["data"] == {"text": "partial source"} and partial["truncated"] and not partial["data_omitted"]
    assert download["data"] == children[2].canonical["value"] and not download["truncated"]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_b4_repair_delivers_original_body_on_wire_without_reexecution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = "BODY_" + secrets.token_hex(16)
    monkeypatch.setattr(probe.gate, "HTML", probe.gate.HTML.replace("Production browser fixture", marker))
    exchange = 0
    directory = tmp_path / "run"
    code = ("for (let i=0;i<2;i++) {"
        "unwrap(await tools.browser_navigate({url:inputs.url}));"
        "let s=unwrap(await tools.browser_snapshot({}));"
        "unwrap(await tools.browser_fill({ref:s.elements.find(e=>e.name==='Locality').ref,value:'福州'}));"
        "s=unwrap(await tools.browser_snapshot({}));"
        "unwrap(await tools.browser_click({ref:s.elements.find(e=>e.name==='Load record').ref}));"
        "unwrap(await tools.browser_wait({condition:'text',value:'鱼丸',timeout_ms:5000}));"
        "unwrap(await tools.browser_read({})); } return {broken:undefined};")

    def scripted(url: str, payload: dict[str, object], headers: dict[str, str], **kwargs: object) -> dict[str, object]:
        nonlocal exchange
        exchange += 1
        observe = kwargs["observe"]
        assert callable(observe)
        observe(attempt=1, data=json.dumps(payload).encode(), phase="request", response_bytes=0, seconds=0.0)
        if exchange == 1:
            action: JsonObject = {"tool": "codemode", "args": {"code": code, "inputs": {"url": URL}}}
        else:
            messages = payload["messages"]
            assert isinstance(messages, list)
            results = [json.loads(content.split("Result data (JSON):\n", 1)[1].split("\n\nResult metadata:", 1)[0])
                for m in messages if isinstance(m, dict) and isinstance(content := m.get("content"), str)
                and content.startswith("[result: codemode]\nResult data (JSON):")]
            recovered = next(result["value"] for result in results if result["value"].get("receipts_replayed"))
            bodies = [row for row in recovered["replayed"] if row["tool"] == "browser_read"]
            assert len(bodies) == 2
            for body in bodies:
                assert marker in body["data"]["text"] and "QUARTZ_851" in body["data"]["text"]
                assert body["receipt_ref"].startswith("task_turn_effect:")
                assert body["status"] == "succeeded" and not body["truncated"] and not body["data_omitted"]
            assert "untrusted" in recovered["notice"]
            action = {"tool": "done", "args": {"summary": bodies[-1]["data"]["text"]}}
        return surface.script_body(action)

    monkeypatch.setattr("codey.providers.api_generation.generate", scripted)
    monkeypatch.setattr("tools.completion_explanation_ab.service_state", lambda _url: {})
    row = probe.run_arm(directory, "page_reset", "B", "B4", TARGET, max_turns=4)
    assert row["passed"] and exchange == 2, row
    metrics = decode_json_object(row["metrics"])
    assert metrics["mechanical_repairs"] == 1 and metrics["repaired_parent"]
    assert metrics["browser_navigate"] == metrics["browser_fill"] == metrics["browser_click"] == 2


def test_optional_perf_failure_does_not_discard_a_successful_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.completion_explanation_ab import ExperimentProvider, service_state

    install_http(monkeypatch, [{"tool": "done", "args": {"summary": "actual successful reply"}}])
    monkeypatch.setattr("tools.completion_explanation_ab.service_state", service_state)

    def broken_get(*args: object, **kwargs: object) -> object:
        raise http.client.IncompleteRead(b"partial", 100)

    monkeypatch.setattr("urllib.request.urlopen", broken_get)
    provider = ExperimentProvider(TARGET, tmp_path, fixed_acquisition=False)
    try:
        body = provider._generate([{"role": "user", "content": "synthetic test"}])
        assert "actual successful reply" in json.dumps(body)
        events = [json.loads(line) for line in (tmp_path / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
        response = next(event for event in events if event.get("type") == "response")
        assert "IncompleteRead" in response["service"]["error"]
        assert not any(event.get("type") == "error" for event in events)
    finally:
        provider.close()


def test_combination_requires_complete_independent_evidence(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="B1, B2, B3 and B4"):
        probe.check_evidence([], TARGET)
    rows: list[JsonObject] = []
    pairs: list[JsonObject] = []
    for repetition in (1, 2):
        for scenario in probe.SCENARIOS["B1"]:
            for arm in ("A", "B"):
                rows.append({"arm": arm, "scenario": scenario, "repetition": repetition, "passed": True})
            pairs.append({"comparable": True})
    assert probe.qualification(rows, pairs, "B1", 2)
    rows[-1]["duplicate_effect"] = True
    assert not probe.qualification(rows, pairs, "B1", 2)
    rows[-1]["duplicate_effect"] = False
    assert not probe.qualification(rows[:-1], pairs, "B1", 2)


@pytest.mark.parametrize("defect", ["none", "model", "source", "harness", "failed", "duplicate_arm"])
def test_combination_checks_evidence_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str) -> None:
    # This test concerns record validation, not repeatedly hashing the entire repository.
    monkeypatch.setattr(probe.edit, "production_source_digest", lambda: "source-fingerprint")
    monkeypatch.setattr(probe, "harness_digests", lambda: {"test.py": "harness-fingerprint"})
    paths: list[Path] = []
    for config in ("B1", "B2", "B3", "B4"):
        root = tmp_path / config
        root.mkdir()
        rows: list[JsonObject] = []
        pairs: list[JsonObject] = []
        for repetition in (1, 2):
            for scenario in probe.SCENARIOS[config]:
                for arm in ("A", "B"):
                    rows.append({"arm": arm, "scenario": scenario, "repetition": repetition, "passed": True})
                pairs.append({"comparable": True})
        report: JsonObject = {"config": config, "version": surface.VERSION, "repeats": 2,
            "runs": list(rows), "pairs": list(pairs), "model": TARGET.model, "seed": TARGET.seed,
            "base_url": TARGET.base_url, "request_timeout": TARGET.timeout,
            "source_before": probe.edit.production_source_digest(), "source_after": probe.edit.production_source_digest(),
            "harnesses": probe.harness_digests(), "harnesses_stable": True}
        if config == "B4":
            if defect == "model":
                report["model"] = "other"
            elif defect == "source":
                report["source_after"] = "changed"
            elif defect == "harness":
                report["harnesses"] = {}
            elif defect == "failed":
                rows[-1]["passed"] = False
            elif defect == "duplicate_arm":
                rows[0]["arm"] = "B"
        probe.edit.write_json(root / "summary.json", report)
        paths.append(root)
    if defect == "none":
        assert len(probe.check_evidence(paths, TARGET)) == 4
    else:
        with pytest.raises(ValueError):
            probe.check_evidence(paths, TARGET)


def test_neutral_task_is_identical_for_both_arms() -> None:
    task = "browser_read 返回字符串，直接 return unwrap(await tools.browser_read({}))，不要再取 .text。读取实际记录。"
    assert probe.task_text(task, "basic") == "读取实际记录。"


def test_matrix_preserves_negative_baseline_and_alternates_pairs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    def worker(directory: Path, scenario: str, arm: str, config: str, target: Target,
               *, budget: float, max_turns: int) -> JsonObject:
        order.append(arm)
        directory.mkdir(parents=True)
        (directory / "task.txt").write_text("same task " + scenario, encoding="utf-8")
        row: JsonObject = {"scenario": scenario, "config": config, "arm": arm,
            "passed": arm == "B" or scenario == "structured", "source_before": "source", "source_after": "source",
            "model_calls": 2, "usage_complete": True, "prompt_tokens": 10, "completion_tokens": 5}
        probe.edit.write_json(directory / "summary.json", row)
        return row

    monkeypatch.setattr(probe, "run_worker", worker)
    monkeypatch.setattr(probe.edit, "production_source_digest", lambda: "source")
    monkeypatch.setattr(probe, "harness_digests", lambda: {"tools/protocol_surface_ab.py": "hash"})
    args = argparse.Namespace(config="B2", run_dir=tmp_path / "matrix", scenario=None, repeats=2,
                              evidence=[], budget=300, max_turns=10)
    assert probe.matrix(args, TARGET) == 1  # A's genuine scoring failures remain failures.
    report = json.loads((args.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert order == ["A", "B", "B", "A", "B", "A", "A", "B"]
    assert report["arms"]["B"]["passed"] == 4 and report["arms"]["A"]["passed"] == 2
    assert report["paired_outcomes"]["B_only"] == 2
    assert report["combination_eligible"] is True
    assert report["arms"]["B"]["prompt_tokens"] == 40
    with pytest.raises(FileExistsError):
        probe.matrix(args, TARGET)


@pytest.mark.parametrize("failure", ["provider_failure", "worker_failure"])
def test_matrix_stops_before_queuing_after_transport_or_worker_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    failure: str) -> None:
    calls: list[str] = []

    def worker(directory: Path, scenario: str, arm: str, config: str, target: Target,
               *, budget: float, max_turns: int) -> JsonObject:
        calls.append(arm)
        return {"arm": arm, "scenario": scenario, "passed": False,
                **({"stop_reason": "provider_failure"} if failure == "provider_failure" else {"error": "worker exited 2"})}

    monkeypatch.setattr(probe, "run_worker", worker)
    monkeypatch.setattr(probe.edit, "production_source_digest", lambda: "source")
    monkeypatch.setattr(probe, "harness_digests", lambda: {})
    args = argparse.Namespace(config="B4", run_dir=tmp_path / "run", scenario=None, repeats=2,
                              evidence=[], budget=300, max_turns=10)
    assert probe.matrix(args, TARGET) == 1
    assert calls == ["A"]
    assert not (args.run_dir / "summary.json").exists()
    interrupted = json.loads((args.run_dir / "interruption.json").read_text(encoding="utf-8"))
    assert interrupted["status"] == "interrupted" and len(interrupted["runs"]) == 1


def test_source_digest_unchanged_and_no_production_patch_leaks(tmp_path: Path) -> None:
    before = probe.edit.production_source_digest()
    with surface.prototype(tmp_path, "basic", "combined"):
        assert probe.edit.production_source_digest() == before
    assert probe.edit.production_source_digest() == before
