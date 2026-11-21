"""The common entry reuses configured verification discovery after project edits."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.operations.context import RunHooks, RunWork
from codey.operations.provider_session import adapt_provider
from codey.operations.task_entry import run_entry_kernel
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.task.model import TaskSubmission
from tests.support.kernel_fixture import admit
from tests.support.task_entry_helpers import entry_frame


def test_hybrid_entry_discovers_the_project_check_and_refreshes_after_edit(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "marker.txt").write_text("old", encoding="utf-8")
    (project / ".codey").mkdir()
    (project / ".codey/config.json").write_text(json.dumps({"schema_version": 1,
        "verification": {"commands": [{"command": "python -m unittest -v", "cwd": "."}]}}), encoding="utf-8")
    (project / "test_marker.py").write_text(
        "import unittest\nfrom pathlib import Path\nclass Check(unittest.TestCase):\n"
        " def test_marker(self): self.assertEqual(Path('marker.txt').read_text(), 'updated')\n", encoding="utf-8")

    class Provider:
        calls = 0

        def send(self, prompt: str, timeout: float | None = None) -> str:
            self.calls += 1
            if self.calls == 1:
                return json.dumps({"tool": "codemode", "args": {"code":
                    "const base=await tools.read_file({path:'marker.txt'});unwrap(base);"
                    "return unwrap(await tools.edit({path:'marker.txt',base_result_ref:base.result_ref,content:'updated'}));",
                    "inputs": {}}})
            if self.calls == 2:
                assert '"command":"python -m unittest -v"' in prompt
                return json.dumps({"tool": "run", "args": {"command": "python -m unittest -v", "path": ".",
                                                              "run_in_background": False}})
            assert self.calls == 3
            return json.dumps({"tool": "done", "args": {"summary": "Updated marker.txt; the local check passed."}})

    task = "Update marker.txt to updated, run the project's local check and report the result."
    request = TaskSubmission("s", str(project), task, 4, False, "local", intent="hybrid", run_id="r")
    frame = entry_frame(request)
    frame.task_kind = "hybrid"
    provider = Provider()
    frame.provider = adapt_provider(provider)
    hooks = RunHooks(lambda _event: None, lambda _request: None, lambda _update: None,
                     lambda _id, _failure: None, lambda _append: None, lambda: (), None)
    with closing(AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)) as state:
        admit(state.durable, run_id="r", session_id="s", task=task, provider_id="local", max_turns=4,
              task_kind="hybrid")
        result = run_entry_kernel(frame, RunWork([], ExecutionEvidence()), hooks, build_task_deps(state), task_kind="hybrid")
        assert result.event["stop_reason"] == "done", result.event.get("summary")
        assert frame.entry_session is not None
        assert any(candidate.command == "python -m unittest -v" for candidate in frame.entry_session.verification_candidates)
        # A displayed project check does not certify coverage of a text file.
        assert frame.entry_session.selected_verification is None
        assert frame.entry_session.verification_candidates_epoch == max(frame.entry_session.edited_files.values())
    assert provider.calls == 3 and (project / "marker.txt").read_text(encoding="utf-8") == "updated"
