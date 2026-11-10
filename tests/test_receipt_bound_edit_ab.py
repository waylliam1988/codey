"""Validate the experiment helper against the production edit executor."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.agents.tools import AgentToolFns
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain import runtime as tool_runtime
from codey.toolchain.runtime import ToolOutcome
from codey.workspace.revision import WorkspaceRevisionStore
from tools.receipt_bound_edit_ab import mentions_helper_call, prototype_helper, provider_metrics


def execute(root: Path, code: str, *, tool_fns: AgentToolFns | None = None) -> tuple[TaskSession, ToolResult]:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(root))
    snapshot = build_turn_snapshot(session)
    revisions = WorkspaceRevisionStore(root.parent / (root.name + "-state"))
    executor = build_execute_turn_executor(
        session=session, snapshot=snapshot, run_id="helper-probe", effect_scope="task", turn=1,
        project_path=root, allow_writes=True, workspace_revision_store=revisions, tool_fns=tool_fns,
    )
    with prototype_helper(root, enabled=True):
        result = execute_turn(
            session, [ToolCall("codemode", {"code": code})], executors={"codemode": executor},
            run_id="helper-probe", effect_scope="task", turn=1, project_path=root, snapshot=snapshot,
            workspace_revision_store=revisions,
        )[0]
    return session, result


def test_helper_binds_the_exact_read_receipt_and_uses_one_existing_edit(tmp_path: Path) -> None:
    (tmp_path / "result.json").write_text('{"amount":0,"preserve":true}\n', encoding="utf-8")
    session, result = execute(tmp_path, "return await update_file('result.json', text => {"
                              "const data=JSON.parse(text); data.amount=173; return JSON.stringify(data); });")
    assert result.ok, result.model_text
    assert json.loads((tmp_path / "result.json").read_text()) == {"amount": 173, "preserve": True}
    reads = [(ref, receipt) for ref, receipt in session._memory_results.items() if receipt.call.name == "read_file"]
    edits = [receipt for receipt in session._memory_results.values() if receipt.call.name == "edit"]
    assert len(reads) == len(edits) == 1
    assert edits[0].call.args["base_result_ref"] == reads[0][0]
    assert edits[0].call.args["path"] == reads[0][1].call.args["path"]


def test_helper_preserves_external_change_and_propagates_edit_failure(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("before\n", encoding="utf-8")

    def concurrent_read(root: Path, path: str, *, offset: int = 1, limit: int = 300) -> ToolOutcome:
        outcome = tool_runtime.read_file(root, path, offset=offset, limit=limit)
        target.write_text("external\n", encoding="utf-8")
        return outcome

    session, result = execute(tmp_path, "await update_file('a.txt', text => text + 'change'); return 'updated';",
                              tool_fns=AgentToolFns(read_file=concurrent_read))
    assert not result.ok
    assert "Read the current file again" in result.model_text
    assert target.read_text() == "external\n"
    edits = [receipt for receipt in session._memory_results.values() if receipt.call.name == "edit"]
    assert len(edits) == 1 and not edits[0].ok  # No implicit reread or retry.


@pytest.mark.parametrize("failure", ["missing", "partial", "transform"])
def test_helper_never_turns_read_or_transform_failure_into_a_write(tmp_path: Path, failure: str) -> None:
    target = tmp_path / "a.txt"
    if failure != "missing":
        target.write_text("line\n" * (400 if failure == "partial" else 1), encoding="utf-8")
    before = target.read_bytes() if target.exists() else None
    transform = "text => { throw Error('cannot transform'); }" if failure == "transform" else "text => text + 'change'"
    session, result = execute(tmp_path, f"return await update_file('a.txt', {transform});")
    assert not result.ok
    assert (target.read_bytes() if target.exists() else None) == before
    assert not any(receipt.ok and receipt.call.name == "edit" for receipt in session._memory_results.values())


def test_usage_counts_duplicate_exchanges_once_and_keeps_missing_usage_unknown(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    request = {"type": "request", "exchange": 1, "recorder_id": "r"}
    response = {"type": "response", "exchange": 1, "recorder_id": "r", "seconds": 1,
                "payload": {"usage": {"prompt_tokens": 10, "completion_tokens": 3}}}
    path.write_text("\n".join(json.dumps(row) for row in (request, response, response)), encoding="utf-8")
    metrics = provider_metrics(path)
    assert metrics["model_calls"] == 1 and metrics["prompt_tokens"] == 10 and metrics["completion_tokens"] == 3
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n" + json.dumps({**request, "exchange": 2}))
    metrics = provider_metrics(path)
    assert metrics["model_calls"] == 2 and metrics["usage_complete"] is False
    assert metrics["prompt_tokens"] is None and metrics["completion_tokens"] is None


def test_helper_adoption_does_not_count_a_definition_or_wrong_tool_namespace() -> None:
    assert mentions_helper_call("return await update_file(inputs.path, text => text);")
    assert not mentions_helper_call("return await tools.update_file({path: inputs.path});")
    assert not mentions_helper_call("async function update_file(path, transform) { return transform(path); }")
