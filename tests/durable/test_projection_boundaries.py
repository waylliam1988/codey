from __future__ import annotations

from pathlib import Path

from codey.app.context import AppContext
from codey.app.server import _transcript_route, _transcript_search_route


def test_ui_state_is_projection_only(tmp_path: Path) -> None:
    state = AppContext(tmp_path / "state")
    try:
        payload = {
            "revision": 0,
            "sessions": [{
                "id": "conversation-ui",
                "messages": [{"id": "ui-message", "type": "user", "text": "draft history"}],
            }],
        }
        state.save_ui_state(payload, base_revision=0)

        state.durable.create_conversation("conversation-ui")
        assert state.durable.entries("conversation-ui") == ()
    finally:
        assert state.close()


def test_history_routes_read_durable_context(tmp_path: Path) -> None:
    state = AppContext(tmp_path / "state")
    try:
        state.durable.create_conversation("conversation-1")
        state.durable.append_message("conversation-1", "user_message", {"text": "durable question"})
        state.durable.append_message("conversation-1", "assistant_message", {"text": "durable answer"})

        status, page = _transcript_route(state, "conversation-1", {})
        assert status == 200
        assert [item["text"] for item in page["items"]] == ["durable question", "durable answer"]

        status, result = _transcript_search_route(state, "conversation-1", {"q": ["answer"]})
        assert status == 200
        assert [hit["entry_id"] for hit in result["hits"]] == [page["items"][1]["id"]]
    finally:
        assert state.close()
