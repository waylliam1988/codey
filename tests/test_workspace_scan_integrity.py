"""Incomplete observations cannot certify a workspace or erase an applied edit."""

from pathlib import Path

from codey.workspace.revision import workspace_fingerprint


def test_unreadable_directory_never_produces_trusted_fingerprint(tmp_path, monkeypatch):
    (tmp_path / "source").mkdir()
    original = Path.iterdir

    def fail_source(path):
        if path.name == "source":
            raise PermissionError("directory unreadable")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", fail_source)
    assert workspace_fingerprint(tmp_path) == ""


def test_failed_root_lstat_is_not_a_trusted_empty_workspace(tmp_path, monkeypatch):
    original = Path.lstat

    def unavailable_root(path):
        if path == tmp_path:
            raise PermissionError("root entry unavailable")
        return original(path)

    monkeypatch.setattr(Path, "lstat", unavailable_root)
    assert workspace_fingerprint(tmp_path) == ""


def test_coverage_rules_are_part_of_identity_even_when_no_file_is_affected(tmp_path):
    (tmp_path / "main.py").write_text("x = 1\n")
    assert workspace_fingerprint(tmp_path) != workspace_fingerprint(tmp_path, ignored_paths=("generated",))


def test_large_file_content_change_with_preserved_size_and_mtime_invalidates_identity(tmp_path):
    import os

    path = tmp_path / "large.bin"
    path.write_bytes(b"a" * (2 * 1024 * 1024 + 1))
    stamp = path.stat()
    before = workspace_fingerprint(tmp_path)
    path.write_bytes(b"b" * stamp.st_size)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert before != workspace_fingerprint(tmp_path)


def test_project_scan_configuration_is_applied_by_identity_authority(tmp_path):
    import json

    (tmp_path / "toolchain").mkdir()
    for i in range(505):
        (tmp_path / "toolchain" / str(i)).mkdir()
    assert workspace_fingerprint(tmp_path) == ""
    (tmp_path / ".codey").mkdir()
    (tmp_path / ".codey/config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scan": {"ignored_paths": ["toolchain"]},
            }
        )
    )
    assert workspace_fingerprint(tmp_path).startswith("sha256:")


def test_applied_edit_retains_mutation_and_reason_when_identity_scan_fails(tmp_path):
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.workspace.revision import WorkspaceRevisionStore

    for i in range(505):
        (tmp_path / str(i)).mkdir()
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"project.write", "control"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=4,
    )
    session.set_workspace_state(7, "sha256:" + "a" * 64)
    store = WorkspaceRevisionStore(tmp_path / ".codey/state")
    calls = []

    def edit(call):
        calls.append(call)
        (tmp_path / "created.py").write_text("x = 1\n")
        return ToolResult(ok=True, call=call, model_text="created", audit={"changed": True})

    args = dict(
        executors={"edit": edit},
        run_id="scan-failure",
        effect_scope="task",
        turn=1,
        project_path=tmp_path,
        workspace_revision_store=store,
    )
    call = ToolCall(name="edit", args={"path": "created.py", "content": "x = 1\n"})
    result = execute_turn(session, [call], **args)[0]
    assert result.audit["changed"] is True
    assert result.canonical["mutation_applied"] is True
    assert result.canonical["identity_status"] == "unconfirmed"
    assert result.canonical["identity_reason"] == "directory_limit"
    assert session.workspace_revision == 0
    # Durable replay must return the applied result, not execute another edit.
    execute_turn(session, [call], **args)
    assert len(calls) == 1
    assert store.current(tmp_path) == 2
    recovered = TaskSession(policy=session.policy, task_kind="project", project=str(tmp_path), max_turns=4)
    recovered.executed = dict(session.executed)
    replay = execute_turn(recovered, [call], **args)[0]
    assert len(calls) == 1
    assert replay.audit["changed"] is True
    assert replay.canonical["mutation_applied"] is True


def test_recheck_and_verify_after_scope_repair_never_reapplies_edit(tmp_path):
    import json

    from codey.operations.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall
    from codey.workspace.revision import WorkspaceRevisionStore

    tree = tmp_path / "toolchain"
    tree.mkdir()
    for i in range(505):
        (tree / str(i)).mkdir()
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=4,
    )
    store = WorkspaceRevisionStore(tmp_path / ".codey/state")

    def turn(n, calls):
        return execute_turn(
            session,
            calls,
            run_id="repair",
            effect_scope="task",
            turn=n,
            project_path=tmp_path,
            workspace_revision_store=store,
        )

    results = turn(
        1,
        [
            ToolCall(name="edit", args={"path": "created.py", "content": "x = 1\n"}),
            ToolCall(name="read_file", args={"path": "created.py"}),
        ],
    )
    assert results[0].canonical["mutation_applied"] is True
    assert results[1].ok, "Read-only rechecking must remain available after an applied edit"
    (tmp_path / ".codey/config.json").write_text(
        json.dumps({"schema_version": 1, "scan": {"ignored_paths": ["toolchain"]}})
    )
    verified = turn(2, [ToolCall(name="run", args={"command": "python -m py_compile created.py"})])[0]
    assert verified.ok, verified.model_text
    assert session.workspace_fingerprint == store.current_state(tmp_path).fingerprint
    assert session.workspace_revision == 2
    assert (tmp_path / "created.py").read_text() == "x = 1\n"
