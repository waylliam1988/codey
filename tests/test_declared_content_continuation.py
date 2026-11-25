"""Pinned source bindings, history compatibility and per-action prerequisites."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_receipts import ordered_browser_progress
from codey.codemode.kernel import build_execute_turn_executor
from codey.codemode.replay import DurableCodeModeReplay
from codey.completion.verification_policy import VerificationCandidate
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.browser.browser_next_step import next_browser_step
from codey.operations.browser.browser_task_handoff import browser_task_handoff
from codey.operations.completion_gate import evaluate
from codey.operations.completion_output import prepare_completion_output
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_receipts import restore_result_receipt
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_effects import KernelEffectSink
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.output_requirement import declaration
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import parse_tool_success_check, require_browser_file_field, require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.task.web_intent import explicit_browser_checks
from codey.types.json_value import JsonObject, parse_json_object
from codey.workspace.revision import WorkspaceRevisionStore
from tests.test_codemode_replay_authority import _durable, fixture
from tests.test_declared_work_continuation import GRANTS, URL, declared_session, record
from tools.browser_experiment_sources import ResumableBrowserSearchProvider as BrowserSearchProvider


@pytest.mark.parametrize("action", ["browser_navigate", "browser_upload", "browser_click"])
def test_declared_action_wait_uses_the_same_prerequisite_as_completion(action: str, tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    checks = (require_tool_success("browser_wait", arguments={"condition": "text", "value": "Ready"},
                                   after="page_actions"),)
    document = context.browser_document
    assert document is not None
    names = {"browser_wait", "browser_read", "browser_snapshot"}
    assert next_browser_step(checks, records=session.executed, results=session._memory_results,
        document=document, names=names) is None
    record(session, action, {}, document)
    assert ordered_browser_progress("browser_wait", "page_actions", records=session.executed,
        results=session._memory_results, document=document).ready
    step = next_browser_step(checks, records=session.executed, results=session._memory_results,
        document=document, names=names)
    assert step is not None and step.call == ToolCall("browser_wait", {
        "condition": "text", "value": "Ready", "timeout_ms": 5000})


@pytest.mark.parametrize("action", ["browser_snapshot", "browser_fill"])
def test_click_stage_wait_does_not_start_during_observation_or_fill(action: str, tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    document = context.browser_document
    assert document is not None
    checks = (require_tool_success("browser_wait", arguments={"condition": "text", "value": "Ready"},
                                   after="page_actions", stage=1),)
    record(session, action, {}, document)
    assert not ordered_browser_progress("browser_wait", "page_actions", records=session.executed,
        results=session._memory_results, document=document, stage=1).ready
    assert next_browser_step(checks, records=session.executed, results=session._memory_results,
        document=document, names={"browser_wait", "browser_read"}) is None


def test_b8fc1529_snapshot_is_preserved_but_rejected_by_current_runtime() -> None:
    fixture = Path(__file__).parent / "fixtures" / "declared_fields_snapshot_b8fc1529.json"
    original = fixture.read_bytes()
    payload = parse_json_object(original.decode("utf-8"))
    assert payload["input_binding"] is not None
    with pytest.raises(ValueError, match="unsupported captured turn snapshot version"):
        restore_snapshot(payload)
    assert fixture.read_bytes() == original


def test_content_menu_and_pinned_sources_survive_snapshot_restore(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    session.task_text += " using content for the whole file."
    original = build_turn_snapshot(session)
    snapshot, _ = browser_task_handoff(session, original, context)
    assert snapshot.input_binding is not None and snapshot.input_binding.source_refs == ("receipt-0",)
    assert {"read_file", "edit"} <= set(snapshot.nested_tool_names)
    assert snapshot.tool_names == original.tool_names
    restored = restore_snapshot(snapshot_description(snapshot))
    assert restored == snapshot
    for name in ("read_tool_result", "done", "browser_navigate"):
        reply: JsonObject = {"tool": name, "args": {}}
        assert normalize_turn(decode_text_turn(json.dumps(reply)), snapshot=restored).protocol_error
    plan = normalize_turn(decode_text_turn('{"tool":"edit","args":{"path":"ledger.data"}}'), snapshot=restored)
    assert not plan.protocol_error
    assert "declared_fields:true" not in str(plan.calls[0].args["code"])


def test_post_edit_required_call_does_not_deadlock_the_edit_entry(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    session.policy = TaskPolicy(GRANTS, required_checks=(*session.policy.required_checks, require_tool_success("run")))
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is not None


def test_content_restore_rejects_a_missing_source_binding(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    session.task_text += " using content for the whole file."
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is not None
    broken = replace(snapshot, input_binding=replace(snapshot.input_binding, source_refs=()))
    with pytest.raises(ValueError, match="invalid captured declared-field"):
        restore_snapshot(snapshot_description(broken))


def test_correct_file_does_not_certify_false_final_json_facts(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    (tmp_path / "ledger.data").write_text('{"balance":173.25,"keep":"00017"}', encoding="utf-8")
    session.record_edit("ledger.data")
    draft = 'The file now contains:\n```json\n{"balance":125.5}\n```'
    verdict = evaluate(session, draft, context=context)
    assert not verdict.complete and "file_answer_mismatch" in str(verdict)
    assert evaluate(session, 'The file contains:\n```json\n{"balance":173.25}\n```', context=context).complete


def test_declared_output_fields_are_assembled_from_the_file_source(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    output = declaration("json", '{"type":"object","properties":{"balance":{"type":"number"}},'
                         '"required":["balance"],"additionalProperties":false}')
    session.policy = TaskPolicy(GRANTS, required_checks=(*session.policy.required_checks, output))
    prepared = prepare_completion_output(session, '{"balance":999}', context)
    assert prepared.text == '{"balance":173.25}'


def test_final_json_check_uses_the_last_assertion_and_explicit_file_identity(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    (tmp_path / "ledger.data").write_text('{"balance":173.25}', encoding="utf-8")
    session.record_edit("ledger.data")
    draft = 'Previous value:\n```json\n{"balance":0}\n```\nSaved value:\n```json\n{"balance":173.25}\n```'
    assert evaluate(session, draft, context=context).complete
    check = require_browser_file_field(url=URL, prefix="Amount: ", suffix=" units.",
        path="other.data", pointer="/balance", value_type="number")
    session.policy = TaskPolicy(GRANTS, required_checks=(*session.policy.required_checks, check))
    (tmp_path / "other.data").write_text('{"balance":173.25}', encoding="utf-8")
    session.record_edit("other.data")
    # An unqualified shared key cannot establish which file is being reported.
    assert evaluate(session, '{"balance":0}', context=context).complete
    assert not evaluate(session, '{"other.data":{"balance":0}}', context=context).complete
    assert evaluate(session, '{"other.data":{"balance":173.25}}', context=context).complete


def test_settled_content_recovery_uses_original_arguments_after_source_and_file_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, context = declared_session(tmp_path)
    session.task_text += " using content for the whole file."
    document = context.browser_document
    assert document is not None
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    from codey.operations.kernel.turn_presentation import refresh_snapshot_contract
    from codey.operations.project.declared_field_input import model_spec

    binding = snapshot.input_binding
    assert binding is not None
    original = next(spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    snapshot = refresh_snapshot_contract(replace(snapshot, input_binding=replace(binding,
        model_spec=model_spec(original, ("ledger.data",)))))
    snapshot = restore_snapshot(snapshot_description(snapshot))
    plan = normalize_turn(decode_text_turn('{"tool":"codemode","args":{"path":"ledger.data"}}'), snapshot=snapshot)
    call = plan.calls[0]
    code = str(call.args["code"]).replace("return unwrap(await tools.edit", "unwrap(await tools.edit")
    call = replace(call, args={**call.args, "code": code + "throw new Error('parent failed after edit');"})
    provider = BrowserSearchProvider(headless=True)
    browser = provider.browser

    def current_document() -> JsonObject:
        return document

    monkeypatch.setattr(browser, "document_identity", current_document)
    research = ResearchTools(search=provider, store=None, changes=None, browser_session=browser)
    revisions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-revisions"))
    try:
        executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="content-history",
            effect_scope="task", turn=1, project_path=tmp_path, research_tools=research,
            allow_writes=True, workspace_revision_store=revisions)
        failed, = execute_turn(session, [call], snapshot=snapshot, executors={"codemode": executor},
            run_id="content-history", effect_scope="task", turn=1, project_path=tmp_path,
            research_tools=research, workspace_revision_store=revisions)
        assert not failed.ok
        edits = [row for row in session._memory_results.values() if row.call.name == "edit"]
        assert len(edits) == 1 and edits[0].ok
        original_args = dict(edits[0].call.args)
        record(session, "browser_read", {}, document, body="Amount: 999.75 units.")
        external = '{"balance":500,"external":"must survive recovery"}'
        target = tmp_path / "ledger.data"
        target.write_text(external, encoding="utf-8")
        replayed = executor(call)
        assert not replayed.ok and "parent failed after edit" in str(replayed.audit.get("runtime_error"))
        assert target.read_text(encoding="utf-8") == external
        edits = [row for row in session._memory_results.values() if row.call.name == "edit"]
        assert len(edits) == 1 and edits[0].call.args == original_args
    finally:
        provider.close()


def test_partial_effect_diagnostic_preserves_the_current_snapshot_without_replay_authority(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    document = context.browser_document
    assert document is not None
    session.policy = TaskPolicy(GRANTS, required_checks=(require_tool_success("browser_click"),))
    fill = record(session, "browser_fill", {}, document)
    observed = record(session, "browser_snapshot", {}, document)
    result = session._memory_results[observed]
    session._memory_results[observed] = replace(result, canonical={"value": {
        "generation": 7, "elements_truncated": False, "complete": True,
        "elements": [{"ref": "new-control", "role": "button", "name": "Choose", "disabled": False}]}})
    failure = ToolResult(ToolCall("codemode", {"code": "bad.value"}), "Result<T> requires unwrap", ok=False,
        audit={"error_code": "result_contract"}, canonical={"child_effect_ids": [fill, observed]})
    _, handoff = browser_task_handoff(session, build_turn_snapshot(session), context, 7, delivered_results=[failure])
    assert "do not restart" in handoff and "new-control" in handoff and "Result<T> requires unwrap" in handoff
    assert "repair_of" not in handoff


@pytest.mark.parametrize("file_changed", [False, True])
def test_content_restart_restores_settled_formal_arguments_from_durable_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_changed: bool,
) -> None:
    class Crash(BaseException):
        pass

    project, state, _, mutations, outputs, revisions, sink, _ = fixture(tmp_path)
    session, context = declared_session(project)
    session.task_text += " using content for the whole file."
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    snapshot = restore_snapshot(snapshot_description(snapshot))
    call, = normalize_turn(decode_text_turn('{"tool":"edit","args":{"path":"ledger.data"}}'), snapshot=snapshot).calls
    original_delivery = DurableCodeModeReplay.record_delivery

    def interrupted(replay: DurableCodeModeReplay, call_id: str, child_index: int | None) -> None:
        original_delivery(replay, call_id, child_index)
        if child_index == 1:
            raise Crash()

    provider = BrowserSearchProvider(headless=True)
    browser = provider.browser

    def current_document() -> JsonObject:
        assert context.browser_document is not None
        return context.browser_document

    monkeypatch.setattr(browser, "document_identity", current_document)
    research = ResearchTools(search=provider, store=None, changes=None, browser_session=browser)
    monkeypatch.setattr(DurableCodeModeReplay, "record_delivery", interrupted)
    try:
        executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="r", effect_scope="outer",
            turn=0, project_path=project, research_tools=research, allow_writes=True, workspace_revision_store=revisions,
            managed_outputs=outputs, intent_sink=sink, durable=_durable(mutations), session_id="s")
        with pytest.raises(Crash):
            execute_turn(session, [call], snapshot=snapshot, executors={"codemode": executor}, run_id="r",
                effect_scope="outer", turn=0, project_path=project, research_tools=research,
                workspace_revision_store=revisions, managed_outputs=outputs, intent_sink=sink, durable=_durable(mutations))
        edit, = [row for row in session._memory_results.values() if row.call.name == "edit"]
        assert edit.ok
        original_args = dict(edit.call.args)
    finally:
        _durable(mutations).close()

    fresh, _ = declared_session(project)
    document = current_document()
    record(fresh, "browser_read", {}, document, body="Amount: 999.75 units.")
    # An already-settled edit must restore even if its source is no longer in memory.
    fresh.executed.pop("receipt-0")
    fresh._memory_results.pop("receipt-0")
    external = '{"balance":500,"external":"survive process restart"}'
    target = project / "ledger.data"
    expected = external if file_changed else '{"balance":173.25,"keep":"00017"}\n'
    target.write_text(expected, encoding="utf-8")
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    durable.recover_scheduler_state()
    if durable.task("r").status == "pending":
        durable.claim_task("r")
    resumed_sink = KernelEffectSink(RuntimeMutationLine(durable=durable), session_id="s", run_id="r",
        provider_id="local", managed_outputs=outputs, durable=durable)
    monkeypatch.setattr(DurableCodeModeReplay, "record_delivery", original_delivery)
    try:
        # Rebuild the same committed facts as the production restart boundary.
        delivered: dict[str, ToolResult] = {}
        for effect in RuntimeEffectStore(durable=durable).load_effects("s", "r"):
            if effect.settlement is None:
                continue
            restored = restore_result_receipt(effect, store=outputs, session_id="s", run_id="r",
                project_path=project, workspace_store=revisions)
            identity = effect.intent.effect_id
            project_execution_record(fresh, identity, restored.call, restored, ok=restored.ok)
            fresh._memory_results[identity] = restored
            delivered[identity] = restored
        executor = build_execute_turn_executor(session=fresh, snapshot=snapshot, run_id="r", effect_scope="outer",
            turn=0, project_path=project, research_tools=research, allow_writes=True, workspace_revision_store=revisions,
            managed_outputs=outputs, intent_sink=resumed_sink, durable=durable, session_id="s", delivered=delivered)
        result, = execute_turn(fresh, [call], snapshot=snapshot, executors={"codemode": executor}, run_id="r",
            effect_scope="outer", turn=0, project_path=project, research_tools=research,
            workspace_revision_store=revisions, managed_outputs=outputs, intent_sink=resumed_sink, durable=durable,
            delivered=delivered)
        if not file_changed:
            assert result.ok, result.model_text
        assert target.read_text(encoding="utf-8") == expected
        recovered, = [row for row in fresh._memory_results.values() if row.call.name == "edit"]
        assert recovered.call.args == original_args
        assert len(durable.effects("r")) == 3
    finally:
        durable.close()
        provider.close()


@pytest.mark.parametrize("task,expected", [
    ("点击继续，调用 browser_wait 等待出现 Ready，最后 browser_read。", [1, 1]),
    ("导航到页面，调用 browser_wait 等待出现 Ready，最后 browser_read。", [0, 0]),
    ("调用 browser_wait 等待出现 Initial；点击继续，调用 browser_wait 等待出现 Ready，最后 browser_read。", [0, 1, 1]),
])
def test_admission_keeps_single_click_and_mixed_navigation_stages(task: str, expected: list[int]) -> None:
    rules = [rule for check in explicit_browser_checks(task) if (rule := parse_tool_success_check(check)) is not None]
    assert [rule.stage for rule in rules] == expected


def test_selected_verification_requires_its_saved_output_before_the_edit_entry(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    command = "python -m unittest -q"
    (tmp_path / "test_build_id.py").write_text(
        "import unittest\nprint('BUILD_ID=84')\n"
        "class BuildTest(unittest.TestCase):\n    def test_ready(self):\n        self.assertTrue(True)\n",
        encoding="utf-8")
    session.task_text = f"Run `{command}` once, then find BUILD_ID in its saved output. " + session.task_text
    session.policy = TaskPolicy(GRANTS | {"project.verify"}, required_checks=session.policy.required_checks)
    session.verification_forbidden = False
    session.selected_verification = VerificationCandidate(command, ".", "task")
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is None and "read_tool_result" in snapshot.tool_names
    assert "run" in snapshot.inventory_names
    revisions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-versions"))
    ran, = execute_turn(session, [ToolCall("run", {"command": command, "path": "."})],
        project_path=tmp_path, run_id="requested-read", turn=1, workspace_revision_store=revisions)
    assert ran.ok, ran.model_text
    run_ref = next(ref for ref, result in session._memory_results.items() if result.call.name == "run")
    read, = execute_turn(session, [ToolCall("read_tool_result", {"result_ref": run_ref, "query": "BUILD_ID"})],
        project_path=tmp_path, run_id="requested-read", turn=2, workspace_revision_store=revisions)
    assert read.ok, read.model_text
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is not None


def test_free_javascript_requirement_keeps_the_original_programming_contract(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    session.task_text += " Write your own JavaScript for the file edit."
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is None
