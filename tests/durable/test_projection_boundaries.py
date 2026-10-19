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


def test_history_route_treats_new_ui_only_chat_as_empty_history(tmp_path: Path) -> None:
    state = AppContext(tmp_path / "state")
    try:
        status, page = _transcript_route(state, "new-chat", {})
        assert status == 200
        assert page["session_id"] == "new-chat"
        assert page["items"] == []
        assert page["has_more"] is False
    finally:
        assert state.close()


def test_durable_app_context_has_no_runtime_jsonl_fact_source(tmp_path: Path) -> None:
    state = AppContext(tmp_path / "state", start_scheduler=False)
    try:
        assert not hasattr(state, "runtime_log")
        state.durable.admit_input(
            conversation_id="conversation-1",
            request_id="request-1",
            task_id="task-1",
            text="hello",
            provider_snapshot={"provider_id": "local"},
        )
        state.runtime_mutations.accept_operation(
            session_id="conversation-1",
            run_id="task-1",
            provider_id="local",
            turn_budget=1,
            max_repair_rounds=0,
        )
        assert not (tmp_path / "state" / "runtime").exists()
        assert state.runtime_operations.load("conversation-1", "task-1") is not None
    finally:
        assert state.close()
