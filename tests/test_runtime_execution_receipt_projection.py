"""Runtime work observations project exact receipts independently of narrative history."""
from types import SimpleNamespace

from codey.operations import kernel_prompt
from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolCall, ToolResult


def test_work_state_retains_failed_check_reference_and_marks_stale_verification():
    session = TaskSession(policy=None, task_text="Keep public API", workspace_fingerprint="new")
    session.executed["exec-17"] = {"name": "run", "exit_code": 1, "ok": False, "workspace_fingerprint": "old"}
    session._memory_results["exec-17"] = ToolResult(ToolCall("run", {"command": "python -m pytest -q", "path": "."}),
                                                    "FAILED", ok=False, audit={"exit_code": 1})
    text = kernel_prompt.working_context(session)
    assert "exec-17" in text and "python -m pytest -q" in text
    assert "1" in text and "stale" in text
    assert "Do not repeat" in text
    assert "Keep public API" in text


def test_result_delivery_includes_existing_receipt_ref():
    session = TaskSession(policy=None)
    result = ToolResult(ToolCall("run", {}, call_id="call-a"), "FAILED", ok=False)
    session._memory_results["exec-17"] = result
    assert "Stored result: exec-17" in kernel_prompt._result_context(result, session)


def test_work_state_is_updated_before_each_actual_kernel_send(monkeypatch):
    from codey.operations.task_loop import _ProviderTurnState, _receive_turn_plan

    provider = SimpleNamespace(set_working_context=lambda text: captured.append(text))
    captured = []
    session = TaskSession(policy=None, task_text="latest intent")
    monkeypatch.setattr("codey.operations.kernel_transport.send_kernel_reply", lambda *args: (_ for _ in ()).throw(RuntimeError("offline")))
    _receive_turn_plan(session, provider, _ProviderTurnState(prompt="next"), SimpleNamespace(), [], native=False,
                       provider_id="local", turn=1, stop_flag=lambda: False, on_event=None, stagnant_turns=None,
                       propagate_provider_failure=False, trace_recorder=None)
    assert captured and "latest intent" in captured[0]


def test_a_denied_or_unknown_command_is_not_reported_as_completed():
    import json
    session = TaskSession(policy=None)
    session.executed["denied"] = {"name": "run", "exit_code": None, "ok": False}
    state = json.loads(kernel_prompt.working_context(session).split("\n", 1)[1].split("\nRead stored", 1)[0])
    assert state["executions"][0]["status"] == "unknown"


def test_runtime_projection_retains_actual_execution_timestamps():
    session = TaskSession(policy=None)
    session.executed["exec-17"] = {"name": "run", "exit_code": 1, "ok": False}
    session._memory_results["exec-17"] = ToolResult(ToolCall("run", {"command": "pytest"}), "FAILED", ok=False,
        audit={"exit_code": 1, "command_started_at": "start", "command_finished_at": "finish"})
    text = kernel_prompt.working_context(session)
    assert '"started_at": "start"' in text and '"finished_at": "finish"' in text


def test_read_file_facts_survive_independently_of_narrative_summaries():
    session = TaskSession(policy=None)
    session.read_files.add("app.py")
    text = kernel_prompt.working_context(session)
    assert '"read_files": ["app.py"]' in text


def test_following_file_reads_do_not_evict_the_last_command_receipt():
    session = TaskSession(policy=None)
    session.executed['pytest-17'] = {'name':'run', 'exit_code':1, 'ok':False}
    session._memory_results['pytest-17'] = ToolResult(ToolCall('run', {'command':'python -m pytest -q'}),
                                                    'FAILED', ok=False)
    for index in range(15):
        session.executed[f'read-{index}'] = {'name':'read_file', 'ok':True}
    text = kernel_prompt.working_context(session)
    assert 'pytest-17' in text
    assert 'python -m pytest -q' in text
