"""Opt-in smoke of real project reads, writes, provenance and subprocesses."""

import os
import tempfile
from pathlib import Path

import pytest


def test_real_workspace_edit_read_verify_and_external_change(tmp_path):
    project_text = os.environ.get("UI_SMOKE_PROJECT")
    if not project_text:
        pytest.skip("Set UI_SMOKE_PROJECT to run the real workspace smoke")
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall
    from codey.workspace.revision import WorkspaceRevisionStore

    root = Path(project_text)
    store = WorkspaceRevisionStore(tmp_path / "state")
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(root),
        max_turns=8,
    )
    with tempfile.TemporaryDirectory(prefix="codey-integrity-", dir=root) as directory:
        path = (Path(directory) / "probe.py").relative_to(root).as_posix()

        def turn(n, calls):
            return execute_turn(
                session,
                calls,
                run_id="real-workspace",
                effect_scope="task",
                turn=n,
                project_path=root,
                workspace_revision_store=store,
            )

        created = turn(1, [ToolCall(name="edit", args={"path": path, "content": "value = '参数正常'\n"})])[0]
        assert created.ok, created.model_text
        assert created.audit["changed"]
        assert session.workspace_fingerprint.startswith("sha256:")
        read = turn(2, [ToolCall(name="read_file", args={"path": path})])[0]
        assert read.ok and "参数正常" in read.model_text
        modified = turn(
            3,
            [
                ToolCall(
                    name="edit",
                    args={
                        "path": path,
                        "replacements": [{"old_string": "value = '参数正常'", "new_string": "value = '修改正常'"}],
                    },
                )
            ],
        )[0]
        assert modified.ok, modified.model_text
        verified = turn(4, [ToolCall(name="run", args={"command": f"python -m py_compile {path}"})])[0]
        assert verified.ok, verified.model_text
        observed = session.workspace_fingerprint
        (root / path).write_text("value = '外部修改'\n", encoding="utf-8")
        assert store.current_state(root).fingerprint != observed
