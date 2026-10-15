"""Every process entry uses the same explicit UTF-8 pipe contract."""

import sys

from codey.runtime.core import cancellation
from codey.runtime.core.output_capture import BoundedByteCapture


def test_shared_spawn_overrides_python_locale_for_both_streams(tmp_path):
    result = cancellation.run_process(
        [sys.executable, "-c", "import sys;print('\u53c2\u6570\u5f02\u5e38');print('\u9519\u8bef',file=sys.stderr)"],
        cwd=tmp_path,
        env={"PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"},
        timeout=15,
        capture_limit_bytes=262144,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "参数异常"
    assert result.stderr.strip() == "错误"


def test_multibyte_capture_truncation_is_explicit_and_does_not_split_characters():
    capture = BoundedByteCapture(head_limit=4, tail_limit=5)
    capture.feed("参数异常".encode())
    result = capture.finish()
    assert result.truncated
    assert "\ufffd" not in result.text
    assert result.omitted_bytes == 6


def test_non_utf8_process_output_is_explicitly_lossy_instead_of_guessed():
    capture = BoundedByteCapture(head_limit=4, tail_limit=4)
    capture.feed(b"ok\xff" + b"x" * 100 + b"tail")
    result = capture.finish()
    assert result.decoding_errors is True
    assert "invalid UTF-8" in result.text
    assert "lossy" in result.text


def test_run_denial_directs_arbitrary_commands_to_approval(tmp_path):
    import pytest

    from codey.policies.run_command_semantics import RunCommandPolicyError, canonical_run_command

    with pytest.raises(RunCommandPolicyError) as caught:
        canonical_run_command(tmp_path, ".", 'python -c "print(1)"')
    assert caught.value.reason_code == "command_not_allowed"
    assert "shell" in caught.value.display
    assert "approval" in caught.value.display


def test_real_project_delegate_preserves_denial_reason_and_next_action(tmp_path):
    from codey.operations.task_execution import ExecutionDelegate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.verify"})), project=str(tmp_path))
    result, ok, _ = ExecutionDelegate(session=session, project_path=tmp_path).execute(
        ToolCall(name="run", args={"command": 'python -c "print(1)"'})
    )
    assert not ok
    assert "shell" in result.model_text
    assert result.canonical["reason_code"] == "command_not_allowed"
    assert result.canonical["next_action"] == "request_shell_approval"
