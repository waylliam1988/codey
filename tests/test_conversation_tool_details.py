import pytest

from codey.app.event_payloads import browser_event_payload, machine_event_payload
from codey.durable import DurableSession, MemoryDurableStore
from codey.runtime.core.models import ToolCall
from codey.runtime.observe.events import RunEvent, run_event_ui_payload
from codey.toolchain.runtime import ToolOutcome
from codey.utils.object_fields import require_object_fields, require_object_rows
from tests.sse_http import capture_events


def test_tool_summary_does_not_destroy_retained_multiline_details() -> None:
    text = "header\n" + "中文细节\n" * 100
    payload = run_event_ui_payload("run", "chat", RunEvent.tool_finished(
        1, ToolCall("read", {"path": "cache.py"}), ToolOutcome(text, True),
    ))
    assert payload is not None
    assert payload["detail"] == text
    assert payload["detail_truncated"] is False
    result = payload["result"]
    assert isinstance(result, str) and len(result) <= 200


def test_detail_limit_is_explicit_and_separate_from_runner_truncation() -> None:
    text = "x" * 100_000
    payload = run_event_ui_payload("run", "chat", RunEvent.tool_finished(
        1, ToolCall("read", {"path": "cache.py"}), ToolOutcome(text, True),
    ))
    assert payload is not None
    detail = payload["detail"]
    assert isinstance(detail, str) and len(detail) == 64_000
    assert payload["detail_truncated"] is True
    assert payload["truncated"] is False


@pytest.mark.parametrize("replay", [False, True])
def test_http_sse_preserves_expanded_file_content_and_preview(replay: bool) -> None:
    text = "first line\n第二行\nlast line\n"
    payload = run_event_ui_payload("run", "chat", RunEvent.tool_finished(
        1, ToolCall("read", {"path": "cache.py"}),
        ToolOutcome(text, True, presentation={"preview": {"line_start": 1, "line_end": 3}}),
    ))
    assert payload is not None
    replay_events: tuple[dict[str, object], ...] = (
        {"type": "info", "text": "earlier event"}, payload,
    ) if replay else ()
    session = DurableSession(MemoryDurableStore())
    try:
        raw_events: object = capture_events(session,
            "/api/events?last_event_id=1" if replay else "/api/events",
            live=() if replay else (payload,),
            replayed=replay_events)
        events = require_object_rows(raw_events)
    finally:
        session.close()
    tool = next(event for event in events if event["type"] == "tool")
    assert tool["detail"] == text
    assert tool["detail_truncated"] is False
    assert tool["kind"] == "read"
    assert tool["preview"] == {"line_start": 1, "line_end": 3}


def test_browser_tool_details_are_bounded_and_machine_summary_stays_compact() -> None:
    event = {
        "type": "tool", "kind": "edit", "result": "summary", "detail": "x" * 70_000,
        "preview": {"diff": "y" * 70_000, "additions": 3, "deletions": True,
                    "line_start": -1, "matches": 2**60, "query": "q" * 900,
                    "has_more": True, "unexpected": "private"},
    }
    public = browser_event_payload(event)
    assert public is not None
    detail = public["detail"]
    assert isinstance(detail, str) and len(detail) == 64_000
    assert public["detail_truncated"] is True
    preview = require_object_fields(public["preview"])
    diff, query = preview["diff"], preview["query"]
    assert isinstance(diff, str) and len(diff) == 64_000
    assert preview["diff_truncated"] is True
    assert preview["additions"] == 3
    assert preview["has_more"] is True
    assert isinstance(query, str) and len(query) == 500
    assert not {"deletions", "line_start", "matches", "unexpected"} & preview.keys()
    machine = machine_event_payload(event)
    assert machine is not None
    assert "detail" not in machine
    assert "preview" not in machine


def test_browser_tool_start_and_legacy_summary_keep_their_available_fields() -> None:
    start = browser_event_payload({"type": "tool_started", "kind": "read", "activity": "Reading"})
    assert start is not None
    assert start["kind"] == "read"
    assert start["activity"] == "Reading"
    legacy = browser_event_payload({"type": "tool", "kind": "read", "result": "old summary"})
    assert legacy is not None
    assert legacy["result"] == "old summary"
    assert "detail" not in legacy
