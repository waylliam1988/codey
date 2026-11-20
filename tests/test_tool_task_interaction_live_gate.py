"""The manual probe must distinguish delivered completions from unobserved work."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.durable.tool_task import ToolTaskInput
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot, offer_browser_tools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject
from tests.test_browser_production_execution import URL, BrowserFixture
from tests.test_browser_production_execution import production_browser as production_browser
from tests.test_tool_task_background import open_runtime
from tools.completion_artifact_audit import EffectReceipt
from tools.tool_task_interaction_live_gate import assess


def report(*, notified: bool, succeeded: bool = True) -> JsonObject:
    return {
        "exit_code": 0, "stop_reason": "done", "source_stable": True,
        "tasks": [{"task_id": "child", "kind": "tool", "status": "completed", "state": {}}],
        "background_observations": [{"delivered_automatically": notified, "snapshot": {
            "task_ref": "child", "status": "completed", "outcome": "succeeded" if succeeded else "failed",
            "result": {"ok": succeeded, "exit_code": 0 if succeeded else 1},
            "output": {"stdout": {"chunks": [{"start": 0, "text": "PIPE_COMPLETE"}]}}}}],
    }


def input_receipts() -> list[EffectReceipt]:
    return [EffectReceipt("input", ToolResult(ToolCall("task_input", {"task_ref": "child", "data": "one\n"}),
                          "delivered", ok=True), None),
            EffectReceipt("late", ToolResult(ToolCall("task_input", {"task_ref": "child", "data": "late"}),
                          "process task is stopped", ok=False), None)]


def test_automatic_formal_delivery_does_not_require_a_second_explicit_read() -> None:
    assert assess("pipe", report(notified=True), input_receipts()) == []


def test_completed_but_undelivered_work_cannot_prove_model_observation() -> None:
    issues = assess("pipe", report(notified=False), input_receipts())
    assert "model did not read a formally successful background result" in issues


def test_automatically_delivered_failure_cannot_be_reported_as_success() -> None:
    issues = assess("pipe", report(notified=True, succeeded=False), input_receipts())
    assert "model did not read a formally successful background result" in issues


def test_explicit_terminal_read_is_accepted_without_automatic_notification() -> None:
    payload = report(notified=False)
    observations = payload["background_observations"]
    assert isinstance(observations, list) and isinstance(observations[0], dict)
    snapshot = observations[0]["snapshot"]
    receipts = [*input_receipts(), EffectReceipt("observed", ToolResult(ToolCall("task_output", {"task_ref": "child"}),
        "completed", ok=True, canonical={"value": snapshot}), None)]
    assert assess("pipe", payload, receipts) == []


@pytest.mark.usefixtures("public_source_dns")
def test_codemode_failure_describes_admission_from_its_receipt_not_execution_success(
    tmp_path: Path, production_browser: BrowserFixture,
) -> None:
    runtime = open_runtime(tmp_path)
    session, tools, browser = production_browser
    try:
        assert browser.navigate(URL).ok
        snapshot = offer_browser_tools(build_turn_snapshot(session, native=True), session)
        executor = build_execute_turn_executor(session=session, snapshot=snapshot,
            run_id="r", effect_scope="", turn=0, project_path=runtime.project, session_id="s",
            intent_sink=runtime.sink, durable=runtime.app.durable, managed_outputs=runtime.app.managed_outputs,
            workspace_revision_store=runtime.app.workspace_revisions, research_tools=tools)
        result = execute_turn(session, [ToolCall("codemode", {
            "code": "unwrap(await tools.browser_wait(inputs)); throw new Error('AFTER_ADMISSION');",
            "inputs": {"condition": "text", "value": "not present", "timeout_ms": 1000,
                       "run_in_background": True}})],
            run_id="r", turn=0, session_id="s", project_path=runtime.project, snapshot=snapshot,
            durable=runtime.app.durable, intent_sink=runtime.sink, managed_outputs=runtime.app.managed_outputs,
            workspace_revision_store=runtime.app.workspace_revisions, research_tools=tools,
            executors={"codemode": executor})[0]
        assert not result.ok and "AFTER_ADMISSION" in result.model_text
        children = runtime.app.durable.owned_tasks("r")
        assert len(children) == 1
        child = children[0]
        request = ToolTaskInput.from_payload(child.state["tool_input"])
        receipt = session._memory_results[request.origin_effect_id]
        assert receipt.kind == "accepted" and receipt.ok
        assert session.executed[request.origin_effect_id]["ok"] is False
        assert not session.verifications
        assert f"{request.origin_effect_id} (browser_wait, ok=True)" in result.model_text
        assert receipt.model_text in result.model_text
        assert child.task_id in result.model_text
    finally:
        assert runtime.app.close()
