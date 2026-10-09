"""Managed command capture must retain the middle before model preview clipping."""
import json
from types import SimpleNamespace

from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.managed_outputs import ManagedOutputStore, run_command_with_managed_output


def test_real_command_middle_is_searchable_without_running_it_again(tmp_path):
    (tmp_path / "test_output.py").write_text('''import unittest
from pathlib import Path
class Output(unittest.TestCase):
    def test_output(self):
        counter = Path('started.txt')
        counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')
        for index in range(1200):
            print('verification marker: checksum abc123' if index == 600 else 'log line ' + 'x' * 440)
''', encoding="utf-8")
    store = ManagedOutputStore(tmp_path / "state")
    command = "python -m unittest discover -v"
    outcome = run_command_with_managed_output(tmp_path, ".", command, permission_profile="coding_writer",
        store=store, session_id="s", run_id="r", tool_id="execution-1")
    assert outcome.ok and outcome.truncated
    session = TaskSession(policy=SimpleNamespace(allows=lambda _: True))
    session._memory_results["execution-1"] = ToolResult(ToolCall("run", {"command": command}),
        outcome.model_text, ok=outcome.ok, truncated=outcome.truncated, audit=dict(outcome.audit))
    delegate = ExecutionDelegate(session=session, managed_outputs=store, session_id="s", run_id="r")
    result, ok, _ = delegate.execute(ToolCall("read_tool_result", {
        "result_ref": "execution-1", "query": "verification marker", "limit": 800}))
    data = json.loads(result.model_text)
    assert ok and "verification marker: checksum abc123" in data["text"]
    assert data["original_output_incomplete"] is False
    assert (tmp_path / "started.txt").read_text() == "1"
