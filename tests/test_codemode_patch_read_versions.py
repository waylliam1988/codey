from pathlib import Path

import pytest

from codey.codemode.patch import PatchPreflightError, preflight_patch
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.runtime import read_file
from codey.types.json_value import JsonObject


def test_patch_uses_its_target_receipt_when_an_unrelated_path_is_stale(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})))
    for path in ("other.txt", "target.txt"):
        (project / path).write_text("before\n", encoding="utf-8")
        outcome = read_file(project, path)
        assert outcome.ok
        call = ToolCall("read_file", {"path": path}, call_id=path)
        session._memory_results[path] = ToolResult(call, outcome.model_text, ok=True, canonical=outcome.canonical)
        session.executed[path] = {
            "name": "read_file", "ok": True, "call_id": path, "excerpt": outcome.model_text,
            "args_digest": "", "argument_digests": {},
        }

    stale = project / "other.txt"
    stale.unlink()
    stale.symlink_to(outside)
    row: JsonObject = {
        "path": "target.txt",
        "replacements": [{"old_string": "before", "new_string": "after"}],
    }
    planned = preflight_patch(project, session, "coding_writer", (row,), (2,), lambda _index: None,
                              ("other.txt", "target.txt"))
    assert planned[0]["path"] == "target.txt"
    assert (project / "target.txt").read_text(encoding="utf-8") == "before\n"
    assert outside.read_text(encoding="utf-8") == "outside\n"

    with pytest.raises(PatchPreflightError, match="escapes project root"):
        preflight_patch(project, session, "coding_writer", ({**row, "path": "other.txt"},), (2,),
                        lambda _index: None, ("other.txt", "target.txt"))
