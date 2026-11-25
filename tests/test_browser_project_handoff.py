"""Project continuation reuses committed browser evidence without a checklist."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.browser.browser_task_handoff import browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_context import KernelContext
from codey.operations.kernel.kernel_protocol import TurnSnapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject
from tests.test_browser_failure_handoff import DOCUMENT, GRANTS, remember
from tests.test_declared_work_continuation import record
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_prerequisite_ab import ROUND, SUMMARY, code_action
from tools.browser_production_live_gate import run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target

EDIT_HANDOFF = "The requested project modification still needs an actual edit receipt."


def project_session(*, declared: bool) -> TaskSession:
    session = TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success("browser_read"),) if declared else ()),
        task_kind="hybrid", task_text="将网页净重和校验码更新到 result.json，保留其他字段。",
        project_changes_required=True)
    record(session, "browser_read", {}, DOCUMENT, body="净重173克，校验码QUARTZ_851。")
    return session


@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("kind", ["chat", "hybrid", "research", "project"])
def test_project_continuation_does_not_depend_on_a_tool_checklist(declared: bool, kind: str) -> None:
    session = project_session(declared=declared)
    session.task_kind = kind
    _, hint = browser_task_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=DOCUMENT))
    assert EDIT_HANDOFF in hint
    assert "frozen edit contract" in hint and "outer read Result.result_ref" in hint


@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("state", ["readonly", "no_read", "failed_read", "truncated",
                                  "uncommitted", "old_document", "no_document", "edited", "denied", "no_codemode"])
def test_project_continuation_requires_authorized_unfinished_work(declared: bool, state: str) -> None:
    session = project_session(declared=declared)
    document: JsonObject | None = DOCUMENT
    if state == "readonly":
        session.project_changes_required = False
    elif state == "no_read":
        session._memory_results.clear()
        session.executed.clear()
    elif state == "failed_read":
        remember(session, "receipt-0", replace(session._memory_results["receipt-0"], ok=False))
    elif state == "truncated":
        remember(session, "receipt-0", replace(session._memory_results["receipt-0"], truncated=True))
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
    assert EDIT_HANDOFF not in hint


@pytest.mark.parametrize("delivery", ["delivered", "uncertain"])
def test_pending_browser_action_precedes_project_continuation(delivery: str) -> None:
    session = project_session(declared=False)
    remember(session, "click", ToolResult(ToolCall("browser_click", {"ref": "observed"}, "click"), "pending",
        ok=delivery == "delivered", canonical={"value": {"delivery": delivery}},
        audit={"browser_document": DOCUMENT, "query_baseline": {"document": DOCUMENT}}))
    _, hint = browser_task_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=DOCUMENT))
    assert EDIT_HANDOFF not in hint
    assert "browser_snapshot" in hint if delivery == "uncertain" else "browser_read" in hint


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_ordinary_hybrid_executes_the_existing_continuation_with_restored_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots: list[TurnSnapshot] = []
    original = KernelContext.request_snapshot

    def capture(context: KernelContext, snapshot: TurnSnapshot, *,
                delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
        result = original(context, snapshot, delivered_results=delivered_results)
        snapshots.append(result[0])
        return result

    monkeypatch.setattr(KernelContext, "request_snapshot", capture)

    def replies() -> Iterator[JsonObject]:
        yield code_action(ROUND)
        snapshot = snapshots[-1]
        binding = snapshot.input_binding
        assert binding is not None and binding.kind == "declared_fields"
        assert binding.source_refs
        spec = binding.model_spec
        assert spec.required == ("path",)
        rejected = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("outside", spec.name, {"path": "other.json"}),)),
                                  snapshot=snapshot)
        assert rejected.protocol_error
        yield {"tool": spec.name, "args": {"path": "result.json"}}
        assert snapshots[-1].input_binding is None
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
