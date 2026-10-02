"""Once dispatched, kernel entry interprets tools without routing again."""
import json
from dataclasses import replace
from types import SimpleNamespace

from codey.agents.tools import DEFAULT_TOOL_FNS
from codey.operations.context import RunWork
from codey.operations.task_entry import run_entry_kernel
from codey.policies.task_policy import TaskPolicy
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from tests.test_auto_direct_answer_continues_to_kernel import _auto_frame, _SeqProvider


def test_auto_submission_at_kernel_entry_executes_first_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    provider = _SeqProvider([
        json.dumps({"tool": "read_file", "args": {"path": "a.py"}}),
        json.dumps({"tool": "done", "args": {"summary": "read"}}),
    ])
    frame = _auto_frame("read a.py", provider)
    frame.project_text = str(tmp_path)
    frame.request = replace(frame.request, project=str(tmp_path))
    frame.entry_policy = TaskPolicy(grants=frozenset({"control", "project.read"}))
    monkeypatch.setattr("codey.operations.task_entry._entry_executors", lambda *args: (tmp_path, DEFAULT_TOOL_FNS, None))
    result = run_entry_kernel(frame, RunWork([], evidence=ExecutionEvidence()),
                              SimpleNamespace(on_event=lambda _: None, on_shell_request=None),
                              SimpleNamespace(state=SimpleNamespace()), task_kind="project")
    assert result.event["stop_reason"] == "done"
    assert frame.entry_session.read_files == {"a.py"}
    assert provider.sends == 2


def test_removed_router_and_recovery_facades_cannot_regrow():
    from codey.operations import project_adapter, task_entry
    from codey.policies import task_policy
    assert not hasattr(task_entry, "_decide_auto")
    assert not hasattr(task_entry, "_entry_apply_auto")
    assert not hasattr(task_entry, "_direct_answer_outcome")
    assert not hasattr(task_entry, "_entry_recovery")
    assert not hasattr(project_adapter, "_recovered_result_for_row")
    assert not hasattr(task_policy, "apply_auto_plan")



def test_retired_verification_driver_is_removed():
    from pathlib import Path

    assert not (Path(__file__).resolve().parents[1] / "codey/agents/verification_driver.py").exists()



def test_no_unused_policy_lookup_or_text_based_recovery_facades():
    from codey.operations import kernel_recovery
    from codey.policies import task_policy

    for name in ("_is_recovery_error_text", "_is_recovery_mismatch_text", "_is_recovery_failed_text"):
        assert not hasattr(kernel_recovery, name), name
    for name in ("_grant_for_tool_name", "_alias_allowed"):
        assert not hasattr(task_policy, name), name
