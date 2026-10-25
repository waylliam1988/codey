"""Runtime work observations project exact receipts independently of narrative history."""
from types import SimpleNamespace

import pytest

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
    from codey.providers.base import ResultMetadata, result_metadata_from_text

    assert result_metadata_from_text(kernel_prompt._result_context(result, session)) == ResultMetadata("exec-17", "run", False)


@pytest.mark.parametrize("encoded", [
    '{"result_ref":"task_turn_effect:abc","tool":"read_file","ok":true}',
    '{"result_ref":"task_turn_effect:abc","tool":"edit","ok":false}',
])
def test_result_metadata_retains_reference_prefix_tool_and_verdict(encoded):
    from codey.providers.base import result_metadata_from_text

    text = "Observed body\n\nResult metadata:\n" + encoded + "\nRead it with read_tool_result."
    metadata = result_metadata_from_text(text)
    assert metadata is not None
    assert metadata.result_ref == "task_turn_effect:abc"
    assert metadata.render() == "Result metadata:\n" + encoded


@pytest.mark.parametrize("frame", [
    'Stored result: task_turn_effect:abc',
    'Result metadata:\n{"result_ref":"abc"}',
    'Result metadata:\n{"result_ref":"abc","tool":"read_file","ok":"true"}',
    'Result metadata:\n{"result_ref":"abc","tool":"read_file","ok":1}',
    'Result metadata:\n{"result_ref":"abc","tool":"","ok":true}',
    'Result metadata:\n{"result_ref":"","tool":"read_file","ok":true}',
    'Result metadata:\n{"result_ref":"abc","tool":"read_file","ok":true,"extra":1}',
    'Result metadata:\n{"result_ref":"abc","result_ref":"other","tool":"read_file","ok":true}',
    'Quoted Result metadata:\n{"result_ref":"abc","tool":"read_file","ok":true}',
    'Result metadata:\n{"result_ref":',
])
def test_invalid_result_metadata_cannot_authorize_omitting_an_observed_body(frame):
    from codey.providers.base import result_metadata_from_text

    assert result_metadata_from_text(frame) is None


def test_work_state_is_updated_before_each_actual_kernel_send(monkeypatch):
    from codey.operations.task_loop import _ProviderTurnState, _receive_turn_plan

    provider = SimpleNamespace()
    captured = []
    session = TaskSession(policy=None, task_text="latest intent")
    def send(*args, **kwargs):
        captured.append(kwargs["observations"])
        raise RuntimeError("offline")
    monkeypatch.setattr("codey.operations.kernel_transport.send_kernel_reply", send)
    _receive_turn_plan(session, provider, _ProviderTurnState(prompt="next"), SimpleNamespace(), [], native=False,
                       provider_id="local", turn=1, stop_flag=lambda: False, on_event=None, stagnant_turns=None,
                       propagate_provider_failure=False, trace_recorder=None, context_scope="request")
    assert captured and "latest intent" in captured[0]


def test_a_denied_or_unknown_command_is_not_reported_as_completed():
    import json
    session = TaskSession(policy=None)
    session.executed["denied"] = {"name": "run", "exit_code": None, "ok": False}
    state = json.loads(kernel_prompt.working_context(session).splitlines()[1])
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
