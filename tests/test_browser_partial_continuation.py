"""Partial browser programs continue from settled children, including successful parents."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from codey.codemode.catalog import snapshot_catalog
from codey.operations.browser_task_handoff import bind_browser_references, browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_prompt import _result_context
from codey.operations.kernel_protocol import TurnSnapshot, build_turn_snapshot, offer_browser_tools
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.research_submission_handoff import research_browser_handoff
from codey.operations.task_session import TaskSession
from codey.policies.tool_requirement import require_tool_success
from codey.research.ledger import OpenedSource
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from tests.test_browser_failure_handoff import next_step, remember
from tests.test_browser_result_adoption_ab import DOCUMENT, session_with_snapshot
from tests.test_declared_work_continuation import declared_session
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_prerequisite_ab import LOAD, code_action
from tools.browser_production_live_gate import run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target


def partial_session(*, parent_ok: bool = True) -> tuple[TaskSession, ToolResult]:
    session = session_with_snapshot(checks=(require_tool_success("browser_wait", arguments={
        "condition": "text", "value": "Ready", "timeout_ms": 5000}, after="page_actions"),))
    remember(session, "fill-receipt", ToolResult(ToolCall("browser_fill", {"ref": "old-input", "value": "chosen"}, "fill"),
        "Filled", ok=True, canonical={"value": {"delivery": "verified"}}, audit={"browser_document": DOCUMENT}))
    remember(session, "click-rejected", ToolResult(ToolCall("browser_click", {"ref": "current-one"}, "click"),
        "The old reference was rejected", ok=False,
        canonical={"value": {"delivery": "not_delivered", "reason": "stale_ref"}},
        audit={"browser_document": DOCUMENT}))
    parent = ToolResult(ToolCall("codemode", {"code": "original program"}, "parent"), "parent outcome", ok=parent_ok,
                       canonical={"child_effect_ids": ["fill-receipt", "click-rejected"]})
    remember(session, "parent", parent)
    return session, parent


def handoff(session: TaskSession, parent: ToolResult, *, current: bool = False) -> tuple[TurnSnapshot, str]:
    if current:
        old = session._memory_results["snapshot"]
        remember(session, "current-controls", replace(old, call=ToolCall("browser_snapshot", {}, "fresh")))
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    context = CompletionContext(browser_document=DOCUMENT)
    snapshot = bind_browser_references(session, snapshot, context, 7 if current else 8)
    return browser_task_handoff(session, snapshot, context, 7 if current else 8, delivered_results=[parent])


@pytest.mark.parametrize("parent_ok", [False, True])
@pytest.mark.parametrize("current", [False, True])
def test_partial_program_uses_child_facts_and_only_the_missing_control_step(parent_ok: bool, current: bool) -> None:
    session, parent = partial_session(parent_ok=parent_ok)
    offered, hint = handoff(session, parent, current=current)
    assert "fill-receipt" in hint and "click-rejected" in hint, hint
    assert "not delivered" in hint and "browser_fill" in hint, hint
    assert "browser_fill" not in offered.tool_names
    assert "browser_fill" in offered.nested_tool_names
    assert restore_snapshot(snapshot_description(offered)) == offered
    assert {entry.name for entry in snapshot_catalog(offered).entries} == set(offered.nested_tool_names)
    if current:
        assert "current-controls" in hint and "current-one" in hint
        assert "browser_click" in offered.tool_names
        assert "Execute this next step:" not in hint
    else:
        assert next_step(hint) == {"tool": "browser_snapshot", "args": {}}
        assert "browser_click" not in offered.tool_names


@pytest.mark.parametrize("later", ["click", "fill", "navigation", "other_page", "uncertain"])
def test_recovered_or_changed_page_does_not_keep_the_old_control_failure_active(later: str) -> None:
    session, parent = partial_session()
    if later == "fill":
        session.policy = replace(session.policy, required_checks=())
    name = "browser_click" if later in {"click", "uncertain"} else "browser_fill" if later == "fill" else "browser_navigate"
    document = {**DOCUMENT, "loader_id": "other"} if later == "other_page" else DOCUMENT
    remember(session, "later", ToolResult(ToolCall(name, {}, "later"), "later action", ok=later != "uncertain",
        canonical={"value": {"delivery": "uncertain" if later == "uncertain" else "delivered"}},
        audit={"browser_document": document}))
    offered, hint = handoff(session, parent)
    assert "click-rejected" not in hint, hint
    if later == "fill":
        assert "browser_fill" in offered.tool_names


@pytest.mark.parametrize("state", ["uncommitted", "uncertain_fill", "failed_fill", "other_document", "denied", "disabled"])
def test_control_recovery_requires_verified_current_document_delivery(state: str) -> None:
    session, parent = partial_session()
    if state == "uncommitted":
        session.executed.pop("fill-receipt")
    elif state == "denied":
        session.executed["click-rejected"]["execution_disposition"] = "denied_before_execution"
    elif state == "disabled":
        rejected = session._memory_results["click-rejected"]
        remember(session, "click-rejected", replace(rejected,
            canonical={"value": {"delivery": "not_delivered", "reason": "disabled_control"}}))
    else:
        fill = session._memory_results["fill-receipt"]
        replacement = replace(fill, ok=state not in {"uncertain_fill", "failed_fill"},
            canonical={"value": {"delivery": "uncertain" if state == "uncertain_fill" else "not_delivered"}},
            audit={"browser_document": {**DOCUMENT, "loader_id": "other"}} if state == "other_document" else fill.audit)
        remember(session, "fill-receipt", replacement)
    _, hint = handoff(session, parent)
    assert "Continue the rejected click" not in hint


def test_recovery_does_not_depend_on_the_latest_delivery_batch() -> None:
    session, parent = partial_session()
    snapshot, expected = handoff(session, parent)
    _, restored = browser_task_handoff(session, restore_snapshot(snapshot_description(snapshot)),
        CompletionContext(browser_document=DOCUMENT), 8)
    assert restored == expected


def test_rejected_later_click_cannot_skip_the_previous_declared_stage() -> None:
    session, parent = partial_session()
    saved = tuple(session._memory_results.items())
    session._memory_results.clear()
    session.executed.clear()
    remember(session, "first-stage-click", ToolResult(ToolCall("browser_click", {}, "first"), "Delivered", ok=True,
        canonical={"value": {"delivery": "delivered"}}, audit={"browser_document": DOCUMENT}))
    for ref, result in saved:
        remember(session, ref, result)
    session.policy = replace(session.policy, required_checks=(require_tool_success("browser_wait", arguments={
        "condition": "text", "value": "Previous stage ready", "timeout_ms": 5000}, after="page_actions", stage=1),))
    _, hint = handoff(session, parent)
    assert "earlier declared browser stage lacks settled evidence" in hint
    assert "Execute this next step:" not in hint


def test_complete_declared_sources_enter_file_work_despite_an_optional_rejected_click(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    partial, _ = partial_session()
    for ref in ("fill-receipt", "click-rejected"):
        result = partial._memory_results[ref]
        remember(session, ref, replace(result, audit={"browser_document": context.browser_document}))
    snapshot, hint = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert snapshot.input_binding is not None
    assert snapshot.input_binding.kind == "declared_fields"
    assert "Continue the rejected click" not in hint


@pytest.mark.parametrize("ordered", [False, True])
def test_only_a_declared_phase_can_hide_remaining_business_actions(ordered: bool) -> None:
    session = session_with_snapshot(checks=(require_tool_success("browser_read",
        after="browser_wait" if ordered else ""),))
    remember(session, "click", ToolResult(ToolCall("browser_click", {}, "click"), "Menu opened", ok=True,
        canonical={"value": {"delivery": "delivered"}}, audit={"browser_document": DOCUMENT}))
    if ordered:
        remember(session, "wait", ToolResult(ToolCall("browser_wait", {}, "wait"), "Ready", ok=True,
            canonical={"value": {"action": "wait", "condition": "text", "matched": True,
                                 "page_url": DOCUMENT["url"]}}, audit={"browser_document": DOCUMENT}))
    offered, hint = browser_task_handoff(session, offer_browser_tools(build_turn_snapshot(session), session),
                                     CompletionContext(browser_document=DOCUMENT), 7)
    assert ("browser_click" in offered.tool_names) is not ordered
    if ordered:
        assert next_step(hint)["tool"] == "browser_read"
    else:
        assert "Execute this next step:" not in hint and "do not click again" not in hint


@pytest.mark.parametrize("complete", [False, True])
def test_successful_program_returning_an_edit_still_delivers_its_plain_browser_read(complete: bool) -> None:
    session = session_with_snapshot(checks=())
    body = "Observed measurement: 271.625 grams; record ID OBSERVED_031."
    url = require_json_string(DOCUMENT["url"], "source URL")
    source = OpenedSource(url, url, "Observed source", "2026-11-16T00:00:00+00:00",
                         hashlib.sha256(body.encode()).hexdigest()[:16], truncated=not complete)
    read = ToolResult(ToolCall("browser_read", {}, "program:child:0"), body, ok=True, truncated=not complete,
        canonical={"research_observation": {"source": source.to_dict(), "text": body, "pages": []},
                   "read_passages": [{"ref": "passage", "start": 0, "end": len(body)}]},
        audit={"browser_document": DOCUMENT})
    parent = ToolResult(ToolCall("codemode", {}, "program"), '{"path":"target.json"}', ok=True,
        canonical={"child_effect_ids": ["read-source"]}, audit={"codemode_execution_id": "program"})
    session._memory_results.update({"read-source": read, "program-result": parent})
    rendered = _result_context(parent, session)
    assert body in rendered and "read-source" in rendered
    assert ('"complete_read": true' in rendered) is complete
    assert parent.model_text == '{"path":"target.json"}' and "query_click" not in read.audit


def test_expected_rejected_click_after_a_delivered_click_does_not_undo_its_wait() -> None:
    session, parent = partial_session()
    fill = session._memory_results["fill-receipt"]
    session._memory_results["fill-receipt"] = replace(fill, call=ToolCall("browser_click", {}, "fill"),
        canonical={"value": {"delivery": "delivered"}})
    project_execution_record(session, "fill-receipt", session._memory_results["fill-receipt"].call,
                             session._memory_results["fill-receipt"], ok=True)
    _, hint = handoff(session, parent)
    assert next_step(hint)["tool"] == "browser_wait"
    assert "do not click again" in hint


@pytest.mark.parametrize("rejected", [False, True])
def test_research_recovers_controls_without_advancing_undeclared_action_prerequisites(rejected: bool) -> None:
    session, _ = partial_session()
    session.task_kind = "research"
    session.required_source_urls = frozenset({require_json_string(DOCUMENT["url"], "page URL")})
    session.policy = replace(session.policy, strict_research=True, required_checks=(*session.policy.required_checks,
        require_tool_success("browser_read"), require_tool_success("browser_fill"), require_tool_success("browser_click")))
    if not rejected:
        session.executed.pop("click-rejected")
        session._memory_results.pop("click-rejected")
    snapshot = build_turn_snapshot(session)
    offered, hint = research_browser_handoff(session, snapshot, CompletionContext(browser_document=DOCUMENT))
    if rejected:
        assert next_step(hint) == {"tool": "browser_snapshot", "args": {}}
        assert "fill-receipt" in hint and "click-rejected" in hint
        assert "browser_fill" not in offered.tool_names
    else:
        assert "Execute this next step:" not in hint


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("only", [False, True])
@pytest.mark.parametrize("current", [False, True])
def test_original_stale_click_program_recovers_without_filling_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, only: bool, current: bool,
) -> None:
    directory = tmp_path / "partial"
    captured: list[str] = []
    original = KernelContext.request_snapshot

    def capture(context: KernelContext, snapshot: TurnSnapshot, *,
                delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
        result = original(context, snapshot, delivered_results=delivered_results)
        captured.append(result[1])
        return result

    monkeypatch.setattr(KernelContext, "request_snapshot", capture)
    fixture = parse_json_object((Path(__file__).parent / "fixtures" / "browser_partial_success_reply_20261116.json").read_text(
        encoding="utf-8"))
    original_args = decode_json_object(fixture["args"])
    code = require_json_string(original_args["code"], "original code")
    if current:
        code = code.replace("return {", "unwrap(await tools.browser_snapshot({}));return {")

    def replies() -> Iterator[JsonObject]:
        yield {"tool": "codemode", "args": {**original_args, "code": code}}
        assert "not delivered" in captured[-1], captured[-1]
        if not current:
            yield next_step(captured[-1])
        assert "Current controls" in captured[-1], captured[-1]
        receipts = tool_receipts(directory / "state")
        observed = next(row.result for row in reversed(receipts) if row.result.call.name == "browser_snapshot")
        elements = decode_json_object(observed.canonical["value"])["elements"]
        assert isinstance(elements, list)
        button = next(row for row in elements if isinstance(row, dict) and row.get("name") == "Load record")
        args: JsonObject = {"ref": button["ref"]}
        if not only:
            args["purpose"] = "result"
        yield ({"tool": "codemode", "args": {"code": "return unwrap(await tools.browser_click(inputs));", "inputs": args}} if only else
               {"tool": "browser_click", "args": args})
        if only:
            yield next_step(captured[-1])
            yield next_step(captured[-1])
        yield {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}

    install_http(monkeypatch, replies())
    row = run_case(directory, "chat", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 15),
        codemode_only=only, max_turns=6 if only else 4, additional_required_tools=(require_tool_success("browser_wait", arguments={
            "condition": "text", "value": "鱼丸", "timeout_ms": 5000}, after="page_actions"),))
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    assert len([row for row in receipts if row.result.call.name == "browser_fill"]) == 1
    clicks = [row.result for row in receipts if row.result.call.name == "browser_click"]
    assert [row.ok for row in clicks] == [False, True]
    assert decode_json_object(clicks[0].canonical["value"])["delivery"] == "not_delivered"
    assert len([row for row in receipts if row.result.call.name == "browser_navigate"]) == 1
    assert len([row for row in receipts if row.result.call.name == "browser_wait"]) == 1
    assert len([row for row in receipts if row.result.call.name == "browser_read"]) == 1
    parents = [row.result for row in receipts if row.result.call.name == "codemode"]
    original_parent = next(parent for parent in parents if parent.call.args.get("code") == code)
    assert original_parent.ok
    assert len([row for row in receipts if row.result.call.name == "browser_snapshot"]) == 2
    # Rendering the original successful parent still reports both child outcomes.
    # The latest observation is chosen by the request handoff, not historical prose.
    session = session_with_snapshot(checks=())
    session._memory_results.clear()
    session.executed.clear()
    for receipt in receipts:
        session._memory_results[receipt.identity] = receipt.result
        project_execution_record(session, receipt.identity, receipt.result.call, receipt.result, ok=receipt.result.ok)
    rendered = _result_context(original_parent, session)
    assert '"settled_actions"' in rendered and '"delivery": "verified"' in rendered
    assert '"delivery": "not_delivered"' in rendered


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_bad_wait_arguments_after_a_delivered_click_resume_at_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "bad-wait"
    captured: list[str] = []
    original = KernelContext.request_snapshot

    def capture(context: KernelContext, snapshot: TurnSnapshot, *,
                delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
        result = original(context, snapshot, delivered_results=delivered_results)
        captured.append(result[1])
        return result

    monkeypatch.setattr(KernelContext, "request_snapshot", capture)

    def replies() -> Iterator[JsonObject]:
        yield code_action(LOAD + "return unwrap(await tools.browser_wait({condition:'text',value:'鱼丸'}));")
        wait = next_step(captured[-1])
        assert wait["tool"] == "browser_wait"
        yield wait
        read = next_step(captured[-1])
        assert read["tool"] == "browser_read"
        yield read
        yield {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}

    install_http(monkeypatch, replies())
    row = run_case(directory, "chat", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 15),
        max_turns=4, additional_required_tools=(require_tool_success("browser_wait", arguments={
            "condition": "text", "value": "鱼丸", "timeout_ms": 5000}, after="page_actions"),))
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    assert len([row for row in receipts if row.result.call.name == "browser_fill"]) == 1
    assert len([row for row in receipts if row.result.call.name == "browser_click"]) == 1
    assert len([row for row in receipts if row.result.call.name == "browser_wait" and row.result.ok]) == 1
    assert any(row.result.call.name == "codemode" and not row.result.ok for row in receipts)
