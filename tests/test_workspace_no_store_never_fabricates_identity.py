"""Missing workspace ownership cannot fabricate a new identity or verify it."""

from types import SimpleNamespace

import pytest

from codey.operations import kernel_provenance
from codey.operations.kernel_execution import execute_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult


def test_missing_store_preserves_previous_identity_after_real_disk_change(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.py").write_text("x = 2\n", encoding="utf-8")
    prior = "sha256:" + "0" * 64
    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.write"})))
    session.set_workspace_state(5, prior)
    evidence = SimpleNamespace(set_workspace_state=lambda *args: pytest.fail("missing store cannot update evidence"))
    assert kernel_provenance.sync_workspace_state_after_edit(session, project, evidence) == (0, "")
    assert (session.workspace_revision, session.workspace_fingerprint) == (5, prior)


def test_missing_store_blocks_same_batch_verification_after_edit(tmp_path):
    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.write", "project.verify"})))
    ran = []

    def edit(call):
        (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
        return ToolResult(call, "edited", ok=True, audit={"changed": True})

    def run(call):
        ran.append(call)
        return ToolResult(call, "passed", ok=True, audit={"exit_code": 0})

    results = execute_turn(session, [
        ToolCall("edit", {"path": "a.py", "content": "x = 2\n"}),
        ToolCall("run", {"path": ".", "command": "python -m pytest"}),
    ], executors={"edit": edit, "run": run}, project_path=tmp_path, run_id="missing-store")
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"
    assert ran == []
    assert len(results) == 2
    assert all(result.model_text.startswith("ERROR:") for result in results)
    assert session.verifications == []


@pytest.mark.parametrize("revision", [True, "1", 1.0])
def test_session_identity_never_washes_invalid_revision(revision):
    session = SimpleNamespace(workspace_revision=revision, workspace_fingerprint="sha256:" + "0" * 64)
    assert kernel_provenance._session_workspace_identity(session)[0] == 0


def test_no_test_only_workspace_sync_or_unused_forwarding_alias():
    for name in ("with_trusted_workspace_state", "_sync_workspace_after_edit", "_disk_workspace_fingerprint"):
        assert not hasattr(kernel_provenance, name)


@pytest.mark.parametrize("missing", [True, False])
def test_unconfirmed_edit_invalidates_identity_across_following_turns(tmp_path, missing):
    from codey.workspace.revision import workspace_fingerprint

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.write", "project.verify"})),
                          project=str(tmp_path))
    session.set_workspace_state(5, workspace_fingerprint(tmp_path))

    def edit(call):
        (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
        return ToolResult(call, "edited", ok=True, audit={"changed": True})

    def fail_bump(*args, **kwargs):
        raise OSError("unavailable")

    store = None if missing else SimpleNamespace(bump_state=fail_bump)
    execute_turn(session, [ToolCall("edit", {"path": "a.py", "content": "x = 2\n"})],
                 executors={"edit": edit}, project_path=tmp_path, workspace_revision_store=store,
                 run_id="unconfirmed", turn=1)
    assert session.workspace_fingerprint == ""
    result = execute_turn(session, [ToolCall("run", {"path": ".", "command": "python -m pytest"})],
                          executors={"run": lambda call: ToolResult(call, "passed", ok=True, audit={"exit_code": 0})},
                          project_path=tmp_path, workspace_revision_store=store, run_id="unconfirmed", turn=2)[0]
    assert kernel_provenance._kernel_workspace_identity_of(result) is None
    assert session.verifications[-1].get("workspace_fingerprint") != workspace_fingerprint(tmp_path)


def test_unconfirmed_edit_fact_survives_real_receipt_recovery(tmp_path):
    from codey.operations.completion_gate import evaluate
    from tests.test_session_log_receipt_recovery_preserves_facts import (
        _dirs,
        _gate_context,
        _new_session,
        _open_runtime,
        _recover_formal,
    )

    project, state, logs = _dirs(tmp_path)
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    _, _, _, store, _, sink = _open_runtime(logs, state, "s", "r", project)
    session = _new_session(project)
    base = store.current_state(project)
    session.set_workspace_state(base.revision, base.fingerprint)

    def edit(call):
        (project / "a.py").write_text("x = 2\n", encoding="utf-8")
        return ToolResult(call, "edited", ok=True, audit={"changed": True})

    execute_turn(session, [
        ToolCall("run", {"path": ".", "command": "python -m pytest"}),
        ToolCall("edit", {"path": "a.py", "content": "x = 2\n"}),
    ], executors={"edit": edit, "run": lambda call: ToolResult(call, "passed", ok=True, audit={"exit_code": 0})},
       project_path=project, workspace_revision_store=None, intent_sink=sink,
       run_id="r", effect_scope="task", turn=1)
    for _ in range(2):
        restored, _, _, _, _ = _recover_formal(project, state, logs, "s", "r")
        assert restored.edited_files == session.edited_files == {"a.py": 1}
        assert evaluate(restored, "done", context=_gate_context(restored)).complete is False
    assert (project / "a.py").read_text(encoding="utf-8") == "x = 2\n"
