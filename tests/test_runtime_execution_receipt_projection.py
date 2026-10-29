"""Runtime work observations project exact receipts independently of narrative history."""

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


def test_work_state_is_updated_before_each_actual_kernel_send():
    from dataclasses import replace
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from codey.operations.task_loop import run_task_kernel
    from tests.support.kernel_restart import paused_batch

    with TemporaryDirectory() as directory, paused_batch(Path(directory), pause=False) as runtime:
        session = runtime.session()
        request = replace(runtime.request, transport=replace(runtime.request.transport, user_task="latest intent"))
        session.task_text = "latest intent"
        result = run_task_kernel(session, request=request)
        assert result.completed, result
        prompts = [body for kind, body in runtime.events if kind == "send"]
        assert len(prompts) == 2
        assert all("Current work observations" in prompt and "latest intent" in prompt for prompt in prompts)
        assert "ORIGINAL_RESULT" in prompts[-1]


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


def test_pending_execution_guidance_uses_receipts_and_disappears_after_success():
    from codey.policies.task_policy import TaskPolicy
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "knowledge.read"}),
        required_checks=("tool_succeeded:knowledge_read",)))
    session.last_done_text = "I read the knowledge."
    session.executed["failed"] = {"name": "knowledge_read", "ok": False}
    pending = kernel_prompt.working_context(session)
    assert "Outstanding declared executions" in pending
    assert "call knowledge_read and receive a successful tool result" in pending
    assert "Displayed examples and failed calls" in pending
    session.executed["successful"] = {"name": "knowledge_read", "ok": True}
    assert "Outstanding declared executions" not in kernel_prompt.working_context(session)


def test_answer_binding_repair_does_not_request_repeating_successful_execution():
    from codey.policies.task_policy import TaskPolicy
    from codey.policies.tool_requirement import require_tool_success
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"}), required_checks=(
        require_tool_success("read_file", answer_pointer="/value", result_pointer="/text"),)))
    session.executed["read"] = {"name": "read_file", "ok": True, "argument_digests": {}}
    assert "Outstanding declared executions" not in kernel_prompt.working_context(session)
