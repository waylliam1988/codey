"""Frozen contracts and receipt dependencies survive phase transitions."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.codemode.kernel import build_execute_turn_executor
from codey.completion.verification_policy import VerificationCandidate
from codey.operations.browser_completion import ordered_browser_progress, post_wait_passages
from codey.operations.browser_next_step import next_browser_step
from codey.operations.browser_task_handoff import browser_task_handoff
from codey.operations.completion_gate import evaluate
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.policies.browser_requirement import BrowserResearchSequence
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import parse_tool_success_check, require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.research.browser_search import BrowserSearchProvider
from codey.research.tools import ResearchTools
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from codey.workspace.revision import WorkspaceRevisionStore
from tests.test_browser_readable_references import binding
from tests.test_declared_work_continuation import GRANTS, URL, declared_session, record


def test_real_5796b29d_snapshot_is_preserved_but_rejected_by_current_runtime() -> None:
    fixture = Path(__file__).parent / "fixtures" / "browser_controls_snapshot_5796b29d.json"
    original = fixture.read_bytes()
    payload = parse_json_object(original.decode("utf-8"))
    assert payload["browser_references"] is not None
    with pytest.raises(ValueError, match="unsupported captured turn snapshot version"):
        restore_snapshot(payload)
    assert fixture.read_bytes() == original


@pytest.mark.parametrize("version", [1, 2])
def test_control_presentation_versions_are_explicit_and_tampering_cannot_select_another_contract(version: int) -> None:
    from codey.operations.kernel_protocol import offer_browser_tools, refresh_snapshot_contract
    from tests.test_browser_result_adoption_ab import session_with_snapshot

    session = session_with_snapshot()
    references = binding()
    if version == 1:
        references = replace(references, presentation_version=1)
    snapshot = refresh_snapshot_contract(replace(offer_browser_tools(build_turn_snapshot(session), session),
                                                browser_references=references))
    payload = snapshot_description(snapshot)
    assert restore_snapshot(payload) == snapshot
    raw = decode_json_object(payload["browser_references"])
    raw["presentation_version"] = 2 if version == 1 else 1
    payload["browser_references"] = raw
    with pytest.raises(ValueError, match="contract text differs"):
        restore_snapshot(payload)
    raw["presentation_version"] = 3
    with pytest.raises(ValueError, match="presentation version"):
        restore_snapshot(payload)


def test_unversioned_d379cc63_control_presentation_restores_as_choices() -> None:
    from codey.operations.kernel_protocol import offer_browser_tools, refresh_snapshot_contract
    from tests.test_browser_result_adoption_ab import session_with_snapshot

    session = session_with_snapshot()
    snapshot = refresh_snapshot_contract(replace(offer_browser_tools(build_turn_snapshot(session), session),
                                                browser_references=binding()))
    payload = snapshot_description(snapshot)
    raw = decode_json_object(payload["browser_references"])
    raw.pop("presentation_version")
    payload["browser_references"] = raw
    assert restore_snapshot(payload) == snapshot


@pytest.mark.parametrize("stage", [0, 1])
@pytest.mark.parametrize("rejected_after_wait", [False, True])
def test_not_delivered_click_preserves_wait_and_read_progress(
    tmp_path: Path, stage: int, rejected_after_wait: bool,
) -> None:
    session, context = declared_session(tmp_path)
    document = context.browser_document
    assert document is not None
    checks = (require_tool_success("browser_wait", after="page_actions", stage=stage,
                                  arguments={"condition": "text", "value": "Ready"}),
              require_tool_success("browser_read", after="browser_wait", stage=stage))
    record(session, "browser_click", {}, document)
    if rejected_after_wait:
        record(session, "browser_wait", {"condition": "text", "value": "Ready"}, document)
    ref = record(session, "browser_click", {"ref": "old"}, document, ok=False)
    session._memory_results[ref] = replace(session._memory_results[ref], canonical={
        "value": {"delivery": "not_delivered", "reason": "stale_ref"}})
    step = next_browser_step(checks, records=session.executed, results=session._memory_results,
        document=document, names={"browser_wait", "browser_read", "browser_snapshot"})
    assert step is not None and step.call is not None
    assert step.call.name == ("browser_read" if rejected_after_wait else "browser_wait")
    if not rejected_after_wait:
        record(session, "browser_wait", {"condition": "text", "value": "Ready"}, document)
    record(session, "browser_read", {}, document, body="Amount: 173.25 units.")
    for check in checks:
        rule = parse_tool_success_check(check)
        assert rule is not None
        assert rule.observe(session.executed, results=session._memory_results, browser_document=document).evaluation.passed
    page = BrowserResearchSequence(URL, "text", "Ready")
    assert post_wait_passages(page, records=session.executed, results=session._memory_results, document=document)[1]
    assert ordered_browser_progress("browser_wait", "page_actions", records=session.executed,
        results=session._memory_results, document=document, stage=stage).ready


@pytest.mark.parametrize("stage", [0, 2])
@pytest.mark.parametrize("source_changed", [False, True])
def test_ordered_source_binding_reaches_real_content_host_and_keeps_receipt_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: int, source_changed: bool,
) -> None:
    session, context = declared_session(tmp_path)
    document = context.browser_document
    assert document is not None
    original = parse_tool_success_check(session.policy.required_checks[0])
    assert original is not None
    field = replace(original, after="browser_wait", stage=stage)
    session.policy = TaskPolicy(GRANTS, required_checks=(field.declaration,))
    session.task_text += " using content for the whole file."
    session.executed.clear()
    session._memory_results.clear()
    for _ in range(stage):
        record(session, "browser_click", {}, document)
    # Use receipt ids whose lexical order differs from their committed order.
    wait = record(session, "browser_wait", {"condition": "text", "value": "Ready"}, document)
    read = record(session, "browser_read", {}, document, body="Amount: 173.25 units.")
    mapping = {ref: f"source-{100 - index}" for index, ref in enumerate(session.executed)}
    session.executed = {mapping[ref]: row for ref, row in session.executed.items()}
    session._memory_results = {mapping[ref]: row for ref, row in session._memory_results.items()}
    snapshot, _ = browser_task_handoff(session, build_turn_snapshot(session), context)
    snapshot = restore_snapshot(snapshot_description(snapshot))
    assert snapshot.input_binding is not None
    assert mapping[wait] in snapshot.input_binding.source_refs and mapping[read] in snapshot.input_binding.source_refs
    assert snapshot.input_binding.source_refs == tuple(session.executed)
    provider = BrowserSearchProvider(headless=True)
    browser = BrowserSession(provider)

    def current_document() -> JsonObject:
        return document

    monkeypatch.setattr(browser, "document_identity", current_document)
    research = ResearchTools(search=provider, store=None, changes=None, browser_session=browser)
    revisions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-versions"))
    try:
        # New source evidence may invalidate a pending copy, but cannot silently
        # change the content generated from its frozen source references.
        if source_changed:
            record(session, "browser_read", {}, document, body="Amount: 999.75 units.")
        plan = normalize_turn(decode_text_turn('{"tool":"codemode","args":{"path":"ledger.data"}}'), snapshot=snapshot)
        assert not plan.protocol_error
        executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="ordered-content",
            effect_scope="task", turn=1, project_path=tmp_path, research_tools=research,
            allow_writes=True, workspace_revision_store=revisions)
        result, = execute_turn(session, plan.calls, snapshot=snapshot, executors={"codemode": executor},
            run_id="ordered-content", effect_scope="task", turn=1, project_path=tmp_path,
            research_tools=research, workspace_revision_store=revisions)
        assert result.ok is (not source_changed), result.model_text
        assert '"balance":' + ("0" if source_changed else "173.25") in (tmp_path / "ledger.data").read_text(encoding="utf-8")
        edit, = [row for row in session._memory_results.values() if row.call.name == "edit"]
        assert '"balance":173.25' in str(edit.call.args["content"])
        assert edit.ok is (not source_changed)
    finally:
        provider.close()


def test_selected_verification_does_not_remove_the_saved_output_write_prerequisite(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    session.task_text = "Run diagnostics once, then find BUILD_ID in its saved output. " + session.task_text
    session.selected_verification = VerificationCandidate("diagnostics", ".", "task")
    snapshot, guidance = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is None
    assert "read_tool_result" in snapshot.tool_names
    assert "requested_output_read" in guidance


@pytest.mark.parametrize("first_fenced,last_fenced", [(True, False), (False, True), (False, False), (True, True)])
@pytest.mark.parametrize("last_value,complete", [("125.5", False), ("173.25", True)])
def test_last_json_assertion_is_selected_by_position_across_all_forms(
    tmp_path: Path, first_fenced: bool, last_fenced: bool, last_value: str, complete: bool,
) -> None:
    session, context = declared_session(tmp_path)
    (tmp_path / "ledger.data").write_text('{"balance":173.25}', encoding="utf-8")
    session.record_edit("ledger.data")
    first = '{"balance":173.25}'
    last = '{"balance":' + last_value + '}'
    if first_fenced:
        first = "```json\n" + first + "\n```"
    if last_fenced:
        last = "```json\n" + last + "\n```"
    text = "Example:\n" + first + "\nSaved value:\n" + last + "\nThe operation is complete."
    assert evaluate(session, text, context=context).complete is complete


@pytest.mark.parametrize("tail,complete", [
    ('{"ledger.data":{"balance":125.5}}', False),
    ('{"ledger.data":{"balance":17325e-2}}', True),
    ('```javascript\nconst example = {"balance":125.5};\n```', True),
    ('{"other.data":{"balance":125.5}}', True),
])
def test_json_assertions_preserve_outer_file_identity_and_ignore_other_code(
    tmp_path: Path, tail: str, complete: bool,
) -> None:
    session, context = declared_session(tmp_path)
    (tmp_path / "ledger.data").write_text('{"balance":173.25}', encoding="utf-8")
    session.record_edit("ledger.data")
    assert evaluate(session, '```json\n{"balance":173.25}\n```\n' + tail, context=context).complete is complete


@pytest.mark.parametrize("delivery", ["not_delivered", "delivered", "uncertain"])
def test_only_proven_not_delivered_actions_preserve_prior_page_evidence(tmp_path: Path, delivery: str) -> None:
    session, context = declared_session(tmp_path)
    document = context.browser_document
    assert document is not None
    record(session, "browser_wait", {"condition": "text", "value": "Ready"}, document)
    action = record(session, "browser_click", {}, document, ok=False)
    session._memory_results[action] = replace(session._memory_results[action], canonical={"value": {"delivery": delivery}})
    record(session, "browser_read", {}, document, body="Amount: 173.25 units.")
    page = BrowserResearchSequence(URL, "text", "Ready")
    assert bool(post_wait_passages(page, records=session.executed, results=session._memory_results, document=document)[1]) is (
        delivery == "not_delivered")


def test_last_bare_json_assertion_preserves_high_precision_number_identity(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    document = context.browser_document
    assert document is not None
    value = "9007199254740993.1234567890123456789"
    record(session, "browser_read", {}, document, body=f"Amount: {value} units.")
    correct = '{"balance":' + value + '}'
    (tmp_path / "ledger.data").write_text(correct, encoding="utf-8")
    session.record_edit("ledger.data")
    prefix = "Example:\n```json\n" + correct + "\n```\nSaved value: "
    assert evaluate(session, prefix + correct, context=context).complete
    assert not evaluate(session, prefix + '{"balance":9007199254740993.1234567890123456788}', context=context).complete
