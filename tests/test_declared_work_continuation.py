"""Task declarations and settled receipts own mechanical continuation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.browser_followups import click_followup_plan
from codey.operations.browser_next_step import next_browser_step
from codey.operations.browser_task_handoff import browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_prompt import working_context
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.research_submission_handoff import research_browser_handoff, research_submission_handoff
from codey.operations.task_guidance import task_guidance_for_policy
from codey.operations.task_session import TaskSession
from codey.operations.work_status import work_status
from codey.policies.browser_requirement import require_browser_research_sequence
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_browser_file_field, require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.ledger import OpenedSource
from codey.research.task_browser_source import TaskBrowserSource
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain import runtime as tool_runtime
from codey.toolchain.constants import STALE_READ_REFERENCE_CODE
from codey.toolchain.runtime import ToolOutcome
from codey.toolchain.tool_spec import thaw_schema_value
from codey.types.json_value import JsonObject, decode_json_object
from codey.workspace.revision import WorkspaceRevisionStore

GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact",
                    "project.read", "project.write"})
URL = "https://example.com/continuation"


def record(session: TaskSession, name: str, args: JsonObject, document: JsonObject,
           *, body: str = "", ok: bool = True) -> str:
    ref = f"receipt-{len(session.executed)}"
    canonical: JsonObject = {"value": {"matched": ok, "page_url": document["url"], "delivery": "delivered"}}
    if body:
        source = OpenedSource(str(document["url"]), str(document["url"]), "Declared source", "2026-11-16T00:00:00Z",
                              hashlib.sha256(body.encode()).hexdigest()[:16])
        canonical = {"research_observation": {"source": source.to_dict(), "text": body, "pages": []},
                     "read_passages": [{"ref": "p" + hashlib.sha256(ref.encode()).hexdigest()[:12],
                                        "start": 0, "end": len(body)}]}
    result = ToolResult(ToolCall(name, args, ref), body or "settled", ok=ok, canonical=canonical,
                        audit={"browser_document": document})
    session._memory_results[ref] = result
    project_execution_record(session, ref, result.call, result, ok=ok)
    return ref


def declared_session(project: Path) -> tuple[TaskSession, CompletionContext]:
    checks = (require_browser_file_field(url=URL, prefix="Amount: ", suffix=" units.",
        path="ledger.data", pointer="/balance", value_type="number"),)
    session = TaskSession(TaskPolicy(GRANTS, required_checks=checks), task_kind="hybrid",
        task_text=f"Copy the declared fields from {URL} to ledger.data.",
        project=str(project), project_changes_required=True, verification_forbidden=True,
        required_source_urls=frozenset({URL}))
    document: JsonObject = {"url": URL, "loader_id": "current"}
    record(session, "browser_read", {}, document, body="Amount: 173.25 units.")
    (project / "ledger.data").write_text('{"balance":0,"keep":"00017"}\n', encoding="utf-8")
    return session, CompletionContext(project=str(project), browser_document=document)


def test_pending_file_work_never_offers_done(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    _, handoff = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert "deliver done" not in handoff
    assert "ledger.data" in handoff


@pytest.mark.parametrize("coverage", ["complete", "partial", "truncated", "wrong_document"])
def test_fixed_copy_uses_only_a_completely_read_query_region(tmp_path: Path, coverage: str) -> None:
    session, context = declared_session(tmp_path)
    session.executed.clear()
    session._memory_results.clear()
    assert context.browser_document is not None
    text = "Header. Amount: 173.25 units. Footer."
    region = "Amount: 173.25 units."
    ref = record(session, "browser_read", {}, context.browser_document, body=text)
    receipt = session._memory_results[ref]
    start, end = text.index(region), text.index(region) + len(region)
    audit: JsonObject = {**decode_json_object(receipt.audit), "query_click": "settled-click",
        "query_region": {"text": region, "visible": True}}
    if coverage == "wrong_document":
        audit["browser_document"] = {"url": URL, "loader_id": "other-document"}
    session._memory_results[ref] = replace(receipt, truncated=coverage == "truncated", audit=audit,
        canonical={**receipt.canonical, "read_passages": [{"ref": "scoped-passage", "start": start,
            "end": end - 1 if coverage == "partial" else end}]})
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert (snapshot.input_binding is not None) is (coverage == "complete")


@pytest.mark.parametrize("kind", ["chat", "hybrid", "project", "research"])
def test_frozen_report_obligation_excludes_legacy_finding_completion_guidance(kind: str) -> None:
    policy = TaskPolicy(GRANTS, strict_research=True, required_checks=("research_submission", "output_json"))
    text = task_guidance_for_policy(policy, task_kind=kind)
    assert "research_plan" not in text
    assert "[fN]" not in text
    assert "report_sections.coverage" not in text
    assert "Final answer must be JSON" in text


def test_legacy_strict_guidance_retains_its_finding_contract() -> None:
    policy = TaskPolicy(GRANTS, strict_research=True)
    text = task_guidance_for_policy(policy, task_kind="hybrid")
    assert "research_plan" in text and "[fN]" in text and "report_sections.coverage" in text


@pytest.mark.parametrize("strict", [False, True])
def test_missing_source_fields_remain_visible_alongside_pending_execution(tmp_path: Path, strict: bool) -> None:
    session, context = declared_session(tmp_path)
    session.policy = replace(session.policy, strict_research=strict,
        required_checks=(*session.policy.required_checks, require_tool_success("run"),
                         *(("research_submission",) if strict else ())))
    assert context.browser_document is not None
    record(session, "browser_read", {}, context.browser_document, body="The requested record has not been loaded.")
    status = work_status(session, context)
    assert status.tools[0].observation.evaluation.reason_code == "observation_selector_unavailable"
    snapshot, hint = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is None
    assert "Amount: " in hint
    assert "rereading the unchanged view cannot load it" in hint.lower()
    assert "Outstanding declared calls: run" in hint
    pending = working_context(session)
    assert "Outstanding declared evidence" in pending and "Amount: " in pending
    assert "rereading the unchanged view cannot load it" in pending.lower()
    assert "Outstanding declared executions" in pending
    assert not evaluate(session, "Finished.", context=context).complete

    record(session, "browser_read", {}, context.browser_document, body="Amount: 173.25 units.")
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is not None and snapshot.input_binding.kind == "declared_fields"
    assert snapshot.input_binding.model_spec.required == ("path",)
    pending = working_context(session)
    assert "Outstanding declared evidence" not in pending
    assert "Outstanding declared executions" in pending


def test_complete_source_offers_frozen_path_only_input(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    binding = snapshot.input_binding
    assert binding is not None and binding.kind == "declared_fields"
    assert binding.model_spec.required == ("path",)
    assert decode_json_object(thaw_schema_value(dict(binding.model_spec.parameters)["path"]))["enum"] == ["ledger.data"]
    restored = restore_snapshot(snapshot_description(snapshot))
    assert restored == snapshot
    args: JsonObject = {"path": "ledger.data"}
    reply: JsonObject = {"tool": "codemode", "args": args}
    native = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("chosen", "codemode", args),)), snapshot=restored)
    text = normalize_turn(decode_text_turn(json.dumps(reply)), snapshot=restored)
    assert not native.protocol_error and native.calls[0].call_id == "chosen"
    assert native.calls[0].args == text.calls[0].args
    assert "base.result_ref" in str(native.calls[0].args["code"])
    invalid_args: tuple[JsonObject, ...] = ({"path": "other.data"},
        {"path": "ledger.data", "base_result_ref": "invented"}, {"code": "return null;"})
    for invalid in invalid_args:
        reply = {"tool": "codemode", "args": invalid}
        assert normalize_turn(decode_text_turn(json.dumps(reply)), snapshot=restored).protocol_error


@pytest.mark.parametrize("navigation", [False, True])
def test_fixed_copy_keeps_report_and_other_authorized_actions_available(tmp_path: Path, navigation: bool) -> None:
    from codey.operations.kernel_protocol import offer_browser_tools

    session, context = declared_session(tmp_path)
    if navigation:
        session.task_text = f"Open {URL} in a browser, then copy the declared fields."
    session.policy = replace(session.policy, strict_research=True, grants=session.policy.grants | {"knowledge.write"},
        required_checks=(*session.policy.required_checks, "research_submission"))
    original = offer_browser_tools(build_turn_snapshot(session), session)
    if navigation:
        assert original.task_navigation_url == URL
    snapshot, hint = browser_task_handoff(session, original, context)
    assert snapshot.input_binding is not None
    assert snapshot.tool_names == original.tool_names
    assert "submit_research_answer" in snapshot.tool_names
    assert "Report submission is still pending" in hint
    assert '{"tool":"codemode","args":{"path":"ledger.data"}}' in hint
    assert snapshot.browser_followups
    assert restore_snapshot(snapshot_description(snapshot)) == snapshot
    proposal = AssistantTurn(tool_calls=(ProviderToolCall("report", "submit_research_answer", {
        "markdown": "The acquired record gives the requested amount [e1].", "state": "answered"}),))
    plan = normalize_turn(proposal, snapshot=snapshot)
    assert not plan.protocol_error and plan.calls[0].name == "submit_research_answer"


@pytest.mark.parametrize("case", ["read_only", "incomplete", "old_document"])
def test_binding_requires_complete_source_and_the_declared_edit_mode(tmp_path: Path, case: str) -> None:
    session, context = declared_session(tmp_path)
    if case == "read_only":
        session.policy = TaskPolicy(GRANTS - {"project.write"}, required_checks=session.policy.required_checks)
    elif case == "incomplete":
        result = session._memory_results["receipt-0"]
        session._memory_results["receipt-0"] = ToolResult(result.call, result.model_text, ok=True,
            canonical=result.canonical, audit=result.audit, truncated=True)
    elif case == "old_document":
        context = replace(context, browser_document={"url": URL, "loader_id": "replacement"})
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is None


def test_current_file_and_required_changes_share_completion_judgment(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    assert not work_status(session, context).ready
    file = tmp_path / "ledger.data"
    file.write_text('{"balance":173.25,"keep":"00017"}\n', encoding="utf-8")
    session.record_edit("ledger.data")
    assert work_status(session, context).ready
    assert evaluate(session, "Copied the declared field.", context=context).complete
    file.write_text('{"balance":999,"keep":"00017"}\n', encoding="utf-8")
    assert not work_status(session, context).ready
    assert not evaluate(session, "Copied the declared field.", context=context).complete
    snapshot, handoff = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is not None and "final answer" not in handoff


@pytest.mark.parametrize("url,condition,value", [(URL, "text", "Ready"),
    ("https://example.org/another-source", "selector", "article[data-ready]")])
def test_action_click_continues_declared_sequence_without_navigation(url: str, condition: str, value: str) -> None:
    checks = (require_browser_research_sequence(url=url, condition=condition, value=value),)
    session = TaskSession(TaskPolicy(GRANTS, required_checks=checks), task_kind="research")
    document: JsonObject = {"url": url, "loader_id": "current"}
    names = {"browser_wait", "browser_read", "browser_snapshot"}
    record(session, "browser_click", {"ref": "current", "purpose": "action"}, document)
    step = next_browser_step(checks, records=session.executed, results=session._memory_results,
                             document=document, names=names)
    assert step is not None and step.call == ToolCall("browser_wait", {
        "condition": condition, "value": value, "timeout_ms": 5000})
    record(session, "browser_wait", {"condition": condition, "value": value, "timeout_ms": 5000}, document)
    step = next_browser_step(checks, records=session.executed, results=session._memory_results,
                             document=document, names=names)
    assert step is not None and step.call == ToolCall("browser_read", {})
    record(session, "browser_read", {}, document, body="Complete source body.")
    assert next_browser_step(checks, records=session.executed, results=session._memory_results,
                             document=document, names=names) is None
    record(session, "browser_click", {"ref": "new", "purpose": "action"}, document)
    step = next_browser_step(checks, records=session.executed, results=session._memory_results,
                             document=document, names=names)
    assert step is not None and step.call is not None and step.call.name == "browser_wait"


def test_wait_stages_and_documents_do_not_share_receipts() -> None:
    checks = tuple(require_tool_success("browser_wait", arguments={"condition": "text", "value": value},
        after="page_actions", stage=stage) for stage, value in ((1, "Prepared"), (2, "Submitted")))
    session = TaskSession(TaskPolicy(GRANTS, required_checks=checks))
    document: JsonObject = {"url": URL, "loader_id": "current"}
    record(session, "browser_click", {}, document)
    record(session, "browser_wait", {"condition": "text", "value": "Prepared", "timeout_ms": 5000}, document)
    record(session, "browser_click", {}, document)
    other: JsonObject = {"url": URL, "loader_id": "other"}
    record(session, "browser_wait", {"condition": "text", "value": "Submitted", "timeout_ms": 5000}, other)
    step = next_browser_step(checks, records=session.executed, results=session._memory_results,
        document=document, names={"browser_wait", "browser_read", "browser_snapshot"})
    assert step is not None and step.call is not None and step.call.args["value"] == "Submitted"


@pytest.mark.parametrize("value,kind,path,field", [
    ("-0.1250", "number", "ledger.data", "balance"),
    ("12345678901234567890.1234567890123456789", "number", "other.json", "quantity"),
    ("1.25e-12", "number", "manifest.data", "weight"),
    ("000173", "string", "codes.json", "identifier"),
    ("173", "integer", "integers.data", "count"),
])
@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("content", [False, True])
def test_fixed_program_preserves_values_and_retries_with_a_new_full_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, kind: str, path: str, field: str, conflict: bool, content: bool,
) -> None:
    session, context = declared_session(tmp_path)
    if content:
        session.task_text += " using content for the whole file."
    check = require_browser_file_field(url=URL, prefix="Amount: ", suffix=" units.",
                                      path=path, pointer="/" + field, value_type=kind)
    session.policy = TaskPolicy(GRANTS, required_checks=(check,))
    document = context.browser_document
    assert document is not None
    record(session, "browser_read", {}, document, body=f"Amount: {value} units.")
    file = tmp_path / path
    baseline = '{"' + field + '":0,"keep":9007199254740993.123456789}\n'
    file.write_text(baseline, encoding="utf-8")
    browser = BrowserSession()
    provider = TaskBrowserSource(browser)

    def current_document() -> JsonObject:
        return document

    monkeypatch.setattr(browser, "document_identity", current_document)
    research = ResearchTools(search=provider, store=None, changes=None, browser_session=browser)
    versions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-revisions"))
    original = tool_runtime._write_text_after_final_path_check
    injected = False

    def concurrent_write(root: Path, rel: str, content: str, *, tool: str,
                         expected_version: str | None = None) -> ToolOutcome | None:
        nonlocal injected
        if conflict and not injected and expected_version is not None:
            file.write_text(baseline.replace('}\n', ',"external":"preserved"}\n'), encoding="utf-8")
            injected = True
        return original(root, rel, content, tool=tool, expected_version=expected_version)

    monkeypatch.setattr(tool_runtime, "_write_text_after_final_path_check", concurrent_write)
    try:
        for turn in range(1, 3 if conflict else 2):
            snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
            snapshot = restore_snapshot(snapshot_description(snapshot))
            assert snapshot.input_binding is not None
            reply: JsonObject = {"tool": "codemode", "args": {"path": path}}
            plan = normalize_turn(decode_text_turn(json.dumps(reply)), snapshot=snapshot)
            assert not plan.protocol_error
            executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="declared",
                effect_scope="task", turn=turn, project_path=tmp_path, research_tools=research,
                allow_writes=True, workspace_revision_store=versions)
            result, = execute_turn(session, plan.calls, snapshot=snapshot, executors={"codemode": executor},
                run_id="declared", effect_scope="task", turn=turn, project_path=tmp_path,
                research_tools=research, workspace_revision_store=versions)
            assert result.ok is (not conflict or turn == 2), (result.model_text, result.audit)
        encoded = json.dumps(value) if kind == "string" else value
        expected = baseline.replace('"' + field + '":0', '"' + field + '":' + encoded)
        if conflict:
            expected = expected.replace('}\n', ',"external":"preserved"}\n')
        assert file.read_text(encoding="utf-8") == expected
        reads = [(ref, row) for ref, row in session._memory_results.items() if row.call.name == "read_file"]
        edits = [row for row in session._memory_results.values() if row.call.name == "edit"]
        assert len(reads) == len(edits) == (2 if conflict else 1)
        for (ref, read), edit in zip(reads, edits, strict=True):
            assert edit.call.args["base_result_ref"] == ref
            assert "content" in edit.call.args
            assert read.ok
        if conflict:
            assert edits[0].audit["error_code"] == STALE_READ_REFERENCE_CODE
            assert reads[0][0] != reads[1][0]
        snapshot, handoff = browser_task_handoff(session, build_turn_snapshot(session), context)
        assert snapshot.input_binding is None and "final answer" in handoff
    finally:
        provider.close()


def test_ambiguous_page_plans_defer_to_the_current_declared_document() -> None:
    second = "https://example.org/independent-page"
    checks = tuple(require_browser_research_sequence(url=url, condition="text", value=value)
                   for url, value in ((URL, "First ready"), (second, "Second ready")))
    names = {"browser_wait", "browser_read", "browser_snapshot"}
    assert "result" not in click_followup_plan(checks, names).plans
    session = TaskSession(TaskPolicy(GRANTS, required_checks=checks))
    document: JsonObject = {"url": second, "loader_id": "second"}
    record(session, "browser_click", {}, document)
    step = next_browser_step(checks, records=session.executed, results=session._memory_results,
                             document=document, names=names)
    assert step is not None and step.call is not None and step.call.args["value"] == "Second ready"


def test_partial_declaration_resumes_after_a_settled_explicit_wait() -> None:
    checks = (require_tool_success("browser_wait"), require_tool_success("browser_read", after="browser_wait"))
    session = TaskSession(TaskPolicy(GRANTS, required_checks=checks))
    document: JsonObject = {"url": URL, "loader_id": "current"}
    names = {"browser_wait", "browser_read", "browser_snapshot"}
    record(session, "browser_click", {}, document)
    step = next_browser_step(checks, records=session.executed, results=session._memory_results,
                             document=document, names=names)
    assert step is not None and step.call is None and "not fully declared" in step.feedback
    record(session, "browser_wait", {"condition": "text", "value": "Ready", "timeout_ms": 8000}, document)
    step = next_browser_step(checks, records=session.executed, results=session._memory_results,
                             document=document, names=names)
    assert step is not None and step.call == ToolCall("browser_read", {})


def test_free_exploration_and_uncertain_delivery_preserve_model_decisions() -> None:
    session = TaskSession(TaskPolicy(GRANTS))
    document: JsonObject = {"url": URL, "loader_id": "current"}
    names = {"browser_wait", "browser_read", "browser_snapshot"}
    ref = record(session, "browser_click", {}, document)
    assert next_browser_step((), records=session.executed, results=session._memory_results,
                             document=document, names=names) is None
    original = session._memory_results[ref]
    uncertain = replace(original, ok=False, canonical={"value": {"delivery": "uncertain"}})
    session._memory_results[ref] = uncertain
    project_execution_record(session, ref, uncertain.call, uncertain, ok=False)
    step = next_browser_step((), records=session.executed, results=session._memory_results,
                             document=document, names=names)
    assert step is not None and step.call == ToolCall("browser_snapshot", {})
    record(session, "browser_snapshot", {}, document)
    step = next_browser_step((), records=session.executed, results=session._memory_results,
                             document=document, names=names)
    assert step is not None and step.call is None and "uncertain" in step.feedback


@pytest.mark.parametrize("case", ["unconfirmed", "retry_limit"])
def test_copy_does_not_repeat_uncertain_or_exhausted_writes(tmp_path: Path, case: str) -> None:
    session, context = declared_session(tmp_path)
    document = context.browser_document
    assert document is not None
    for _ in range(1 if case == "unconfirmed" else 3):
        ref = record(session, "edit", {"path": "ledger.data", "declared_fields": True}, document, ok=False)
        original = session._memory_results[ref]
        session._memory_results[ref] = replace(original,
            canonical={"mutation_applied": True} if case == "unconfirmed" else {},
            audit={**original.audit, "error_code": "workspace_unconfirmed" if case == "unconfirmed" else STALE_READ_REFERENCE_CODE})
    snapshot, handoff = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is None
    assert ("Verify settlement" if case == "unconfirmed" else "retry limit") in handoff


def test_restored_copy_binding_cannot_expand_its_declared_paths(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    payload = snapshot_description(snapshot)
    binding = decode_json_object(payload["input_binding"])
    spec = decode_json_object(binding["model_spec"])
    spec["parameters"] = [["path", {"type": "string", "enum": ["other.data"]}]]
    binding["model_spec"] = spec
    payload["input_binding"] = binding
    with pytest.raises(ValueError, match="invalid captured declared-field"):
        restore_snapshot(payload)


def test_ordered_wait_does_not_preempt_the_first_observation_or_navigation_recovery() -> None:
    checks = (require_tool_success("browser_wait", arguments={"condition": "text", "value": "Ready"},
                                   after="page_actions", stage=1),)
    session = TaskSession(TaskPolicy(GRANTS, required_checks=checks))
    document: JsonObject = {"url": URL, "loader_id": "current"}
    record(session, "browser_navigate", {"url": URL}, document)
    names = {"browser_wait", "browser_read", "browser_snapshot"}
    assert next_browser_step(checks, records=session.executed, results=session._memory_results,
                             document=document, names=names) is None


@pytest.mark.parametrize("missing", ["source", "saved_output"])
def test_pending_prerequisites_block_copy_and_completion_guidance(tmp_path: Path, missing: str) -> None:
    session, context = declared_session(tmp_path)
    if missing == "source":
        session.required_source_urls |= {"https://example.org/second-required-source"}
    else:
        session.task_text = "Run diagnostics once, then find BUILD_ID in its saved output. " + session.task_text
    status = work_status(session, context)
    assert not status.ready and any(row.status == "not_run" for row in status.other)
    snapshot, handoff = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is None and "before editing" in handoff
    assert "final answer" not in handoff
    assert not evaluate(session, "Completed the task.", context=context).complete


def test_generic_project_edit_guidance_uses_the_frozen_edit_contract(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    session.policy = TaskPolicy(GRANTS, required_checks=(require_tool_success("browser_read"),))
    session.task_text = f"Read {URL} in the browser, then update app.py to use the documented setting."
    snapshot, handoff = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is None
    assert "frozen edit contract" in handoff and "declared_fields" not in handoff
    assert "final answer" not in handoff


def test_strict_research_resumes_each_declared_stage_and_submits_its_latest_passages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = tuple(check for stage, value in ((1, "Prepared"), (2, "Submitted")) for check in (
        require_tool_success("browser_wait", arguments={"condition": "text", "value": value},
                             after="page_actions", stage=stage),
        require_tool_success("browser_read", after="browser_wait", stage=stage)))
    session = TaskSession(TaskPolicy(GRANTS | {"knowledge.write"}, strict_research=True, required_checks=checks), task_kind="research",
                          task_text=f"Read both declared stages on {URL}.", required_source_urls=frozenset({URL}))
    document: JsonObject = {"url": URL, "loader_id": "current"}
    context = CompletionContext(browser_document=document)
    for stage, value in ((1, "Prepared"), (2, "Submitted")):
        record(session, "browser_click", {"purpose": "action"}, document)
        snapshot, handoff = research_browser_handoff(session, build_turn_snapshot(session), context)
        assert "browser_wait" in snapshot.tool_names and value in handoff, handoff
        assert ("Submitted" if stage == 1 else "Prepared") not in handoff
        record(session, "browser_wait", {"condition": "text", "value": value, "timeout_ms": 5000}, document)
        _, handoff = research_browser_handoff(session, build_turn_snapshot(session), context)
        assert '"tool": "browser_read"' in handoff
        record(session, "browser_read", {}, document, body=f"Stage {stage} complete.")
    browser = BrowserSession()

    def current_document() -> JsonObject:
        return document

    monkeypatch.setattr(browser, "document_identity", current_document)
    snapshot, handoff = research_submission_handoff(session, build_turn_snapshot(session), context, browser)
    assert {"codemode", "submit_research_answer"} <= set(snapshot.tool_names), handoff
    assert "tool checklist does not declare the whole investigation complete" in handoff
    assert "[e2]" in handoff and "[e1]" not in handoff


@pytest.mark.parametrize("mutation", ["browser_eval", "browser_scroll", "browser_download"])
@pytest.mark.parametrize("workflow", [False, True])
def test_research_continuation_repeats_wait_after_evidence_is_invalidated(mutation: str, workflow: bool) -> None:
    checks = ((require_tool_success("browser_wait", arguments={"condition": "text", "value": "Ready"}),
               require_tool_success("browser_read", after="browser_wait")) if workflow else
              (require_browser_research_sequence(url=URL, condition="text", value="Ready"),))
    session = TaskSession(TaskPolicy(GRANTS, required_checks=checks))
    document: JsonObject = {"url": URL, "loader_id": "current"}
    names = {"browser_wait", "browser_read", "browser_snapshot"}
    record(session, "browser_click", {}, document)
    record(session, "browser_wait", {"condition": "text", "value": "Ready", "timeout_ms": 5000}, document)
    record(session, "browser_read", {}, document, body="Complete body before mutation.")
    record(session, mutation, {}, document)
    step = next_browser_step(checks, records=session.executed, results=session._memory_results,
                             document=document, names=names, workflow=workflow)
    assert step is not None and step.call == ToolCall("browser_wait", {
        "condition": "text", "value": "Ready", "timeout_ms": 5000})
    record(session, "browser_wait", {"condition": "text", "value": "Ready", "timeout_ms": 5000}, document)
    step = next_browser_step(checks, records=session.executed, results=session._memory_results,
                             document=document, names=names, workflow=workflow)
    assert step is not None and step.call == ToolCall("browser_read", {})


@pytest.mark.parametrize("declaration", ["tool", "explicit_timeout", "sequence"])
def test_settled_wait_uses_declared_predicate_instead_of_the_generated_default_timeout(declaration: str) -> None:
    arguments: JsonObject = {"condition": "text", "value": "Ready"}
    if declaration == "explicit_timeout":
        arguments["timeout_ms"] = 5000
    checks = ((require_browser_research_sequence(url=URL, condition="text", value="Ready"),)
              if declaration == "sequence" else (
                  require_tool_success("browser_wait", arguments=arguments, after="page_actions"),
                  require_tool_success("browser_read", after="browser_wait")))
    session = TaskSession(TaskPolicy(GRANTS, required_checks=checks))
    document: JsonObject = {"url": URL, "loader_id": "current"}
    record(session, "browser_click", {}, document)
    record(session, "browser_wait", {"condition": "text", "value": "Ready", "timeout_ms": 8000}, document)
    step = next_browser_step(checks, records=session.executed, results=session._memory_results,
                             document=document, names={"browser_wait", "browser_read", "browser_snapshot"})
    expected = (ToolCall("browser_wait", {**arguments, "timeout_ms": 5000})
                if declaration == "explicit_timeout" else ToolCall("browser_read", {}))
    assert step is not None and step.call == expected
