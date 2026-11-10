from pathlib import Path

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.codemode.runtime import NodeWorkerRuntime
from codey.codemode.types import ToolCatalogEntry
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_prompt import _result_context
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall
from codey.workspace.revision import WorkspaceRevisionStore


@pytest.mark.parametrize("recover", [False, True])
def test_child_failure_remains_visible_without_overriding_program_outcome(tmp_path: Path, recover: bool) -> None:
    target = tmp_path / "a.txt"
    target.write_text("before\n", encoding="utf-8")
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(tmp_path))
    snapshot = build_turn_snapshot(session)
    revisions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-state"))
    executor = build_execute_turn_executor(
        session=session, snapshot=snapshot, run_id="feedback", effect_scope="task", turn=0,
        project_path=tmp_path, allow_writes=True, workspace_revision_store=revisions,
    )
    code = "await tools.edit({path:inputs.path,content:inputs.content}); "
    if recover:
        code += ("const read=await tools.read_file({path:inputs.path}); "
                 "unwrap(await tools.edit({path:inputs.path,content:inputs.content,base_result_ref:read.result_ref})); ")
    code += "return 'program completed';"
    result = execute_turn(
        session, [ToolCall("codemode", {"code": code, "inputs": {"path": "a.txt", "content": "after\n"}})],
        executors={"codemode": executor}, run_id="feedback", effect_scope="task", turn=0,
        project_path=tmp_path, snapshot=snapshot, workspace_revision_store=revisions,
    )[0]
    assert result.ok, result.model_text
    rendered = _result_context(result, session)
    assert "Child failure receipts" in rendered
    assert "readResult.result_ref" in rendered
    failures = [child for child in session._memory_results.values() if child.call.name == "edit" and not child.ok]
    assert len(failures) == 1
    reference = next(key for key, value in session._memory_results.items() if value is failures[0])
    assert reference in rendered
    assert "failures" not in result.canonical
    assert target.read_text(encoding="utf-8") == ("after\n" if recover else "before\n")


@pytest.mark.parametrize("expression", ["undefined", "NaN", "() => 1", "[undefined]"])
def test_invalid_arguments_are_not_silently_removed_or_coerced(expression: str) -> None:
    result = NodeWorkerRuntime().execute(
        f"return await tools.edit({{base_result_ref:{expression}}});",
        catalog=(ToolCatalogEntry(name="edit", description="edit", parameters={"type": "object", "properties": {
            "base_result_ref": {"type": "string", "description": "Use readResult.result_ref."}}}),),
        invoke=lambda *_: pytest.fail("invalid JSON arguments must not execute a child"),
    )
    assert not result.ok and result.error_code == "argument_encoding"
    assert '$["base_result_ref"]' in result.error


def test_undefined_argument_feedback_uses_the_original_field_contract() -> None:
    result = NodeWorkerRuntime().execute(
        "return await tools.edit({base_result_ref:undefined});",
        catalog=(ToolCatalogEntry(name="edit", description="edit", parameters={"type": "object", "properties": {
            "base_result_ref": {"type": "string", "description": "Use readResult.result_ref."}}}),),
        invoke=lambda *_: pytest.fail("undefined must not execute"),
    )
    assert "Field contract: Use readResult.result_ref." in result.error

