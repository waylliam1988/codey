"""Project continuation reuses committed browser evidence without a checklist."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from codey.codemode.json_edit import JSON_FIELD_EDIT_GUIDANCE, JSON_FIELD_EDIT_PROGRAM
from codey.operations.browser_task_handoff import browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_protocol import TurnSnapshot, build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject
from tests.test_browser_failure_handoff import DOCUMENT, GRANTS, remember
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_prerequisite_ab import ROUND, SUMMARY, code_action
from tools.browser_production_live_gate import run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target


def project_session(*, declared: bool) -> TaskSession:
    session = TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success("browser_read"),) if declared else ()),
        task_kind="hybrid", task_text="将网页净重和校验码更新到 result.json，保留其他字段。",
        project_changes_required=True)
    remember(session, "body", ToolResult(ToolCall("browser_read", {}, "body"),
        "净重173克，校验码QUARTZ_851。", ok=True, canonical={"value": "净重173克，校验码QUARTZ_851。"},
        audit={"browser_document": DOCUMENT}))
    return session


@pytest.mark.parametrize("declared", [False, True])
def test_project_continuation_does_not_depend_on_a_tool_checklist(declared: bool) -> None:
    session = project_session(declared=declared)
    _, hint = browser_task_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=DOCUMENT))
    assert JSON_FIELD_EDIT_GUIDANCE in hint
    assert "inputs.changes" in hint and "actual edit receipt" in hint


@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("state", ["readonly", "chat", "research", "no_read", "failed_read", "truncated",
                                  "uncommitted", "old_document", "no_document", "edited", "denied", "no_codemode"])
def test_project_continuation_requires_authorized_unfinished_work(declared: bool, state: str) -> None:
    session = project_session(declared=declared)
    document: JsonObject | None = DOCUMENT
    if state == "readonly":
        session.project_changes_required = False
    elif state in {"chat", "research"}:
        session.task_kind = state
    elif state == "no_read":
        session._memory_results.clear()
        session.executed.clear()
    elif state == "failed_read":
        remember(session, "body", replace(session._memory_results["body"], ok=False))
    elif state == "truncated":
        remember(session, "body", replace(session._memory_results["body"], truncated=True))
    elif state == "uncommitted":
        session.executed.clear()
    elif state == "old_document":
        document = {**DOCUMENT, "loader_id": "other-document"}
    elif state == "no_document":
        document = None
    elif state == "edited":
        remember(session, "edit", ToolResult(ToolCall("edit", {}, "edit"), "saved", ok=True))
    elif state == "denied":
        session.controller_denied = ("edit",)
    elif state == "no_codemode":
        session.controller_denied = ("codemode",)
    _, hint = browser_task_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=document))
    assert JSON_FIELD_EDIT_GUIDANCE not in hint


@pytest.mark.parametrize("delivery", ["delivered", "uncertain"])
def test_pending_browser_action_precedes_project_continuation(delivery: str) -> None:
    session = project_session(declared=False)
    remember(session, "click", ToolResult(ToolCall("browser_click", {"ref": "observed"}, "click"), "pending",
        ok=delivery == "delivered", canonical={"value": {"delivery": delivery}},
        audit={"browser_document": DOCUMENT, "query_baseline": {"document": DOCUMENT}}))
    _, hint = browser_task_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=DOCUMENT))
    assert JSON_FIELD_EDIT_GUIDANCE not in hint
    assert "browser_snapshot" in hint if delivery == "uncertain" else "browser_read" in hint


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_ordinary_hybrid_executes_the_existing_continuation_with_restored_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    hints: list[str] = []
    original = KernelContext.request_snapshot

    def capture(context: KernelContext, snapshot: TurnSnapshot, *,
                delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
        result = original(context, snapshot, delivered_results=delivered_results)
        hints.append(result[1])
        return result

    monkeypatch.setattr(KernelContext, "request_snapshot", capture)

    def replies() -> Iterator[JsonObject]:
        yield code_action(ROUND)
        assert JSON_FIELD_EDIT_GUIDANCE in hints[-1]
        yield {"tool": "codemode", "args": {"code": JSON_FIELD_EDIT_PROGRAM, "inputs": {
            "path": "result.json", "changes": {"amount": 173, "code": "QUARTZ_851"}}}}
        assert JSON_FIELD_EDIT_GUIDANCE not in hints[-1]
        yield {"tool": "done", "args": {"summary": SUMMARY}}

    install_http(monkeypatch, replies())
    directory = tmp_path / "run"
    row = run_case(directory, "hybrid", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
        declare_required_tools=False, restore_snapshots=True, max_turns=3)
    assert row["passed"] is True, row
    assert row["restored_snapshots"]
    for name in ("browser_click", "browser_read", "read_file", "edit"):
        selected = [receipt.result for receipt in tool_receipts(directory / "state") if receipt.result.call.name == name]
        assert len(selected) == 1 and selected[0].ok, name
