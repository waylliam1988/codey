from __future__ import annotations

import pytest

from codey.operations.kernel_execution import execute_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall
from codey.workspace.revision import WorkspaceRevisionStore


def read(session, root, path="a.py", **options):
    result = execute_turn(session, [ToolCall("read_file", {"path": path, **options})], run_id="r", turn=len(session.executed), project_path=root)[0]
    assert result.ok
    return next(key for key, value in session._memory_results.items() if value is result)


def update(session, root, ref, content="new\n", path="a.py"):
    return execute_turn(session, [ToolCall("edit", {"path": path, "content": content, "base_result_ref": ref})],
                        run_id="r", turn=len(session.executed) + 10, project_path=root,
                        workspace_revision_store=WorkspaceRevisionStore(root.parent / (root.name + "-state")))[0]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_whole_file_update_uses_original_full_read_receipt(tmp_path, newline):
    (tmp_path / "a.py").write_bytes(b"old" + newline)
    session = TaskSession(TaskPolicy(frozenset({"project.read", "project.write"})))
    ref = read(session, tmp_path)
    assert update(session, tmp_path, ref).ok
    assert (tmp_path / "a.py").read_text() == "new\n"
    assert not update(session, tmp_path, ref, content="new\n").ok  # stale baseline even for a no-op


@pytest.mark.parametrize("content", ["current\n", "next\n"])
def test_stale_version_feedback_requires_a_new_observation_before_retry(tmp_path, content):
    (tmp_path / "a.py").write_text("old\n")
    session = TaskSession(TaskPolicy(frozenset({"project.read", "project.write"})))
    old_ref = read(session, tmp_path)
    (tmp_path / "a.py").write_text("current\n")
    result = update(session, tmp_path, old_ref, content)
    assert not result.ok and "Read the current file again with read_file" in result.model_text
    assert "old receipt cannot authorize" in result.model_text
    assert (tmp_path / "a.py").read_text() == "current\n"
    assert update(session, tmp_path, read(session, tmp_path), content).ok
    assert (tmp_path / "a.py").read_text() == content


def test_invalid_program_return_exposes_only_invoked_frozen_types_and_child_receipt_references(tmp_path):
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel_protocol import build_turn_snapshot

    (tmp_path / "a.py").write_text("old\n")
    session = TaskSession(TaskPolicy(frozenset({"project.read", "project.write"})), project=str(tmp_path))
    snapshot = build_turn_snapshot(session)
    revisions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-state"))
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="return-type", effect_scope="task",
        turn=1, project_path=tmp_path, allow_writes=True, workspace_revision_store=revisions)
    result = execute_turn(session, [ToolCall("codemode", {
        "code": "const r=await tools.read_file({path:'a.py'}); "
                "const w=await tools.edit({path:'a.py',content:inputs.text,base_result_ref:r.result_ref}); "
                "return {payload:w.value.text};",
        "inputs": {"text": "updated\n"}})], run_id="return-type", effect_scope="task", turn=1,
        project_path=tmp_path, executors={"codemode": parent}, snapshot=snapshot,
        workspace_revision_store=revisions)[0]
    assert not result.ok and result.audit["error_code"] == "result_encoding"
    assert '$["payload"]' in result.model_text
    assert "tools.edit: Result<string>" in result.model_text and "tools.read_file: Result<" in result.model_text
    assert "tools.grep:" not in result.model_text and "declare const tools" not in result.model_text
    references = result.canonical["child_effect_ids"]
    assert len(references) == 2 and all(ref in result.model_text for ref in references)
    children = [row for row in session.executed.values() if row["name"] != "codemode"]
    assert len(children) == 2 and all(row["ok"] for row in children)
    assert (tmp_path / "a.py").read_text() == "updated\n"


@pytest.mark.parametrize("case", ["partial", "different_file", "missing_ref", "truncated_ref", "deleted", "changed", "readonly"])
def test_whole_file_update_rejects_unproven_or_stale_baselines(tmp_path, case):
    (tmp_path / "a.py").write_text("one\ntwo\n")
    (tmp_path / "b.py").write_text("other\n")
    grants = {"project.read"} if case == "readonly" else {"project.read", "project.write"}
    session = TaskSession(TaskPolicy(frozenset(grants)))
    ref = read(session, tmp_path, **({"limit": 1} if case == "partial" else {}))
    if case == "missing_ref":
        ref = "missing"
    if case == "truncated_ref":
        assert ":" in ref
        ref = ref.rsplit(":", 1)[1]
    if case == "deleted":
        (tmp_path / "a.py").unlink()
    if case == "changed":
        (tmp_path / "a.py").write_text("external\n")
    result = update(session, tmp_path, ref, path="b.py" if case == "different_file" else "a.py")
    assert not result.ok
    assert (tmp_path / "b.py").read_text() == "other\n"
    if case != "deleted":
        assert (tmp_path / "a.py").read_text() == ("external\n" if case == "changed" else "one\ntwo\n")


@pytest.mark.parametrize("reference", ["result_ref", "base_result_ref"])
@pytest.mark.parametrize("content", ["after\n", "'quotes' \"double\" \\n\nnext\r\n\u2028\u2029馃榾\n"])
def test_codemode_whole_file_update_reuses_child_receipt_in_same_runtime(tmp_path, reference, content):
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel_protocol import build_turn_snapshot

    (tmp_path / "a.py").write_bytes(b"before\n")
    session = TaskSession(TaskPolicy(frozenset({"project.read", "project.write"})), project=str(tmp_path))
    snapshot = build_turn_snapshot(session)
    revisions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-state"))
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="code", effect_scope="task",
        turn=1, project_path=tmp_path, allow_writes=True, workspace_revision_store=revisions)
    result = execute_turn(session, [ToolCall("codemode", {"code":
        "const r=await tools.read_file({path:'a.py'}); if(!r.ok) throw Error(r.error); "
        f"const w=await tools.edit({{path:'a.py',content:inputs.content,base_result_ref:r.{reference}}}); "
        "if(!w.ok) throw Error(w.error); return 'updated';", "inputs": {"content": content}})],
        run_id="code", effect_scope="task", turn=1,
        project_path=tmp_path, executors={"codemode": parent}, snapshot=snapshot, workspace_revision_store=revisions)[0]
    if reference == "result_ref":
        assert result.ok, result.model_text
        edits = [receipt for receipt in session._memory_results.values() if receipt.call.name == "edit"]
        assert len(edits) == 1 and edits[0].call.args["content"] == content
        # The original writer intentionally preserves the target's LF style;
        # the input bridge itself must deliver the unchanged mixed-EOL text.
        assert (tmp_path / "a.py").read_bytes() == content.replace("\r\n", "\n").encode("utf-8")
    else:
        assert not result.ok
        error = result.model_text.split("Current codemode contract:")[0]
        assert "readResult.result_ref" in error and "not readResult.value.result_ref" in error
        assert (tmp_path / "a.py").read_text() == "before\n"
