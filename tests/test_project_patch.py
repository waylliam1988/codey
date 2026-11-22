from pathlib import Path

import pytest

from codey.agents.tools import AgentToolFns
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall
from codey.toolchain import runtime
from codey.toolchain.runtime import ToolOutcome
from codey.workspace.revision import WorkspaceRevisionStore


@pytest.mark.parametrize("scenario", ["absent", "syntax", "conflict", "success"])
def test_patch_preflights_all_files_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for name in ("first.txt", "second.py"):
        (project / name).write_bytes(b"before\r\nkeep\n")
    if scenario == "conflict":
        final_check = runtime._write_text_after_final_path_check

        def concurrent_write(root: Path, rel: str, content: str, *, tool: str, expected_version: str | None = None) -> ToolOutcome | None:
            if rel == "second.py":
                (root / rel).write_bytes(b"external\n")
            return final_check(root, rel, content, tool=tool, expected_version=expected_version)

        monkeypatch.setattr(runtime, "_write_text_after_final_path_check", concurrent_write)
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(project))
    snapshot = build_turn_snapshot(session)
    revisions = WorkspaceRevisionStore(tmp_path / "state")
    executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="patch", effect_scope="test",
        turn=1, project_path=project, allow_writes=True, tool_fns=AgentToolFns(), workspace_revision_store=revisions)
    result = execute_turn(session, [ToolCall("codemode", {"inputs": {
        "anchor": "absent" if scenario == "absent" else "before",
        "replacement": "const invalid = 1;" if scenario == "syntax" else "after",
    }, "code": """
        const first = await tools.read_file({path: 'first.txt'});
        await tools.read_file({path: 'second.py'});
        return unwrap(await tools.edit({files: [
            {path: 'first.txt', base_result_ref: first.result_ref, replacements: [{old_string: 'before', new_string: 'after'}]},
            {path: 'second.py', replacements: [{old_string: inputs.anchor, new_string: inputs.replacement}]}
        ]}));
    """})], snapshot=snapshot, run_id="patch", effect_scope="test", turn=1, project_path=project,
        workspace_revision_store=revisions, executors={"codemode": executor})[0]
    if scenario == "success":
        assert result.ok, result.model_text
        expected = b"after\r\nkeep\n"
        assert {row.call.args["path"] for row in session._memory_results.values() if row.call.name == "edit" and row.ok} == {
            "first.txt", "second.py",
        }
    elif scenario == "conflict":
        assert not result.ok and "Patch stopped after writing first.txt" in result.model_text
        assert (project / "first.txt").read_bytes() == b"after\r\nkeep\n"
        assert (project / "second.py").read_bytes() == b"external\n"
        assert session.edited_files.keys() == {"first.txt"}
        return
    else:
        assert not result.ok and "Patch preflight stopped" in result.model_text
        expected = b"before\r\nkeep\n"
        assert all(row.call.name != "edit" for row in session._memory_results.values())
    assert (project / "first.txt").read_bytes() == (project / "second.py").read_bytes() == expected
