from codey.runtime.core.models import ToolCall
from codey.runtime.observe.events import RunEvent, run_event_ui_payload
from codey.toolchain.runtime import ToolOutcome


def test_tool_summary_does_not_destroy_retained_multiline_details():
    text = "header\n" + "中文细节\n" * 100
    payload = run_event_ui_payload("run", "chat", RunEvent.tool_finished(
        1, ToolCall("read", {"path": "cache.py"}), ToolOutcome(text, True),
    ))
    assert payload["detail"] == text
    assert payload["detail_truncated"] is False
    assert len(payload["result"]) <= 200


def test_detail_limit_is_explicit_and_separate_from_runner_truncation():
    text = "x" * 100_000
    payload = run_event_ui_payload("run", "chat", RunEvent.tool_finished(
        1, ToolCall("read", {"path": "cache.py"}), ToolOutcome(text, True),
    ))
    assert len(payload["detail"]) == 64_000
    assert payload["detail_truncated"] is True
    assert payload["truncated"] is False
