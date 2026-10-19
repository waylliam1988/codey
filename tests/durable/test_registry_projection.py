from __future__ import annotations

from pathlib import Path

from codey.app.context import AppContext


def test_provider_window_registry_is_memory_only(tmp_path: Path) -> None:
    first = AppContext(tmp_path / "state")
    try:
        context = first.conversation_for("conversation-1")
        context.begin_window("local", "chat")
        context.record_exchange("hello", "world")
    finally:
        assert first.close()

    second = AppContext(tmp_path / "state")
    try:
        assert not second.conversation_for("conversation-1").initialized
        assert not (tmp_path / "state" / "conversation.sqlite3").exists()
    finally:
        assert second.close()
