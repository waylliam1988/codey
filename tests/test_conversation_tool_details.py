import pytest

from codey.app.event_payloads import browser_event_payload, machine_event_payload
from codey.durable import DurableSession, MemoryDurableStore
from codey.runtime.core.models import ToolCall
from codey.runtime.observe.events import RunEvent, run_event_ui_payload
from codey.toolchain.runtime import ToolOutcome
from tests.sse_http import capture_events


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


@pytest.mark.parametrize("replay", [False, True])
def test_http_sse_preserves_expanded_file_content_and_preview(replay):
    text = "first line\n第二行\nlast line\n"
    payload = run_event_ui_payload("run", "chat", RunEvent.tool_finished(
        1, ToolCall("read", {"path": "cache.py"}),
        ToolOutcome(text, True, presentation={"preview": {"line_start": 1, "line_end": 3}}),
    ))
    events = capture_events(DurableSession(MemoryDurableStore()),
        "/api/events?last_event_id=1" if replay else "/api/events",
        live=() if replay else (payload,),
        replayed=({"type": "info", "text": "earlier event"}, payload) if replay else ())
    tool = next(event for event in events if event["type"] == "tool")
    assert tool["detail"] == text
    assert tool["detail_truncated"] is False
    assert tool["kind"] == "read"
    assert tool["preview"] == {"line_start": 1, "line_end": 3}


def test_browser_tool_details_are_bounded_and_machine_summary_stays_compact():
    event = {
        "type": "tool", "kind": "edit", "result": "summary", "detail": "x" * 70_000,
        "preview": {"diff": "y" * 70_000, "additions": 3, "deletions": True,
                    "line_start": -1, "matches": 2**60, "query": "q" * 900,
                    "has_more": True, "unexpected": "private"},
    }
    public = browser_event_payload(event)
    assert len(public["detail"]) == 64_000
    assert public["detail_truncated"] is True
    assert len(public["preview"]["diff"]) == 64_000
    assert public["preview"]["diff_truncated"] is True
    assert public["preview"]["additions"] == 3
    assert public["preview"]["has_more"] is True
    assert len(public["preview"]["query"]) == 500
    assert not {"deletions", "line_start", "matches", "unexpected"} & public["preview"].keys()
    assert "detail" not in machine_event_payload(event)
    assert "preview" not in machine_event_payload(event)


def test_browser_tool_start_and_legacy_summary_keep_their_available_fields():
    start = browser_event_payload({"type": "tool_started", "kind": "read", "activity": "Reading"})
    assert start["kind"] == "read"
    assert start["activity"] == "Reading"
    legacy = browser_event_payload({"type": "tool", "kind": "read", "result": "old summary"})
    assert legacy["result"] == "old summary"
    assert "detail" not in legacy
