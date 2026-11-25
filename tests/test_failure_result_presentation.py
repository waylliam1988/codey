"""Bounded displays preserve decisive errors without becoming execution facts."""
from __future__ import annotations

import threading

import pytest

from codey.operations.kernel_prompt import _result_context
from codey.operations.task_session import TaskSession
from codey.operations.tool_task_delivery import NOTIFICATION_MAX_CHARS, pending_tool_task_notifications
from codey.operations.tool_task_reads import tool_task_snapshot
from codey.operations.tool_tasks import run_tool_task
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import decode_json_object, parse_json_object, require_json_string
from codey.utils.failure_output import failure_excerpt
from tests.test_tool_task_background import Runtime
from tests.test_tool_task_background import runtime as runtime


@pytest.mark.parametrize("limit", [0, 20, 120, 180, 600])
def test_excerpt_never_cuts_a_failure_line_and_marks_omission(limit: int) -> None:
    lines = [f"FAILED test_checkout.py::test_requirement_{index} - assert 10 == 20" for index in range(7)]
    rendered = failure_excerpt("\n".join([*lines, "7 failed, 3 passed in 0.1s"]), limit=limit)
    assert len(rendered) <= limit
    assert all(line in lines for line in rendered.splitlines() if line.startswith("FAILED "))
    if 120 <= limit < 600:
        assert "omitted" in rendered
    if limit == 600:
        assert all(line in rendered for line in lines)


def test_terminal_notification_shows_all_seven_failures_without_duplicate_raw_log(runtime: Runtime) -> None:
    source = "import pytest\n\n@pytest.mark.parametrize('number', range(10))\ndef test_requirement(number):\n"
    source += "    print('setup noise ' * 200)\n    assert number >= 7\n"
    (runtime.project / "test_requirements.py").write_text(source, encoding="utf-8")
    ref = runtime.accept("python -m pytest -q -s")
    child = runtime.app.durable.claim_task_checkpoint(ref)
    run_tool_task(runtime.app.durable, child, stores=runtime.app.task_submission_stores, signal=threading.Event())
    assert runtime.app.managed_outputs is not None
    notification = pending_tool_task_notifications(runtime.app.durable, owner_task_id="r",
        store=runtime.app.managed_outputs, results={})
    assert notification.refs == (ref,)
    value = parse_json_object(notification.text)
    result = decode_json_object(value["result"])
    text = require_json_string(result["text"], "text")
    assert result["ok"] is False and result["exit_code"] == 1
    assert "7 failed, 3 passed" in text
    assert all(f"test_requirement[{index}]" in text for index in range(7))
    assert "output" not in value
    assert "setup noise" not in text
    assert len(notification.text) <= NOTIFICATION_MAX_CHARS
    # Explicit reads still expose the same receipt and raw output, with no execution.
    snapshot = tool_task_snapshot(runtime.app.durable, runtime.app.durable.task(ref),
        store=runtime.app.managed_outputs, args={})
    assert snapshot["execution_ref"] == value["execution_ref"]
    assert snapshot["outcome"] == value["outcome"] == "failed"
    assert "output" in snapshot
    assert snapshot == tool_task_snapshot(runtime.app.durable, runtime.app.durable.task(ref),
        store=runtime.app.managed_outputs, args={})
    short = tool_task_snapshot(runtime.app.durable, runtime.app.durable.task(ref),
        store=runtime.app.managed_outputs, args={"limit": 180})
    short_text = require_json_string(decode_json_object(short["result"])["text"], "text")
    assert "omitted" in short_text
    assert all(" - " in line for line in short_text.splitlines() if line.startswith("FAILED "))


def test_complete_result_keeps_reference_without_a_long_generic_reading_tutorial() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read"})))
    result = ToolResult(ToolCall("read_file", {"path": "a.py"}), "VALUE = 1", ok=True)
    session._memory_results["task_turn_effect:example"] = result
    text = _result_context(result, session)
    assert "VALUE = 1" in text and "task_turn_effect:example" in text
    assert len(text) < 400
