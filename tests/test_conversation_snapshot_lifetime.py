"""A delayed cache callback cannot restore a forgotten conversation."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from codey.agents.handoff import ConversationSnapshot
from codey.app.context import AppContext


@pytest.mark.parametrize("action", ["forget", "recreate", "evict", "unchanged"])
def test_delayed_snapshot_save_respects_the_current_context_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    entered, release = Event(), Event()
    original = AppContext._save_durable_conversation_snapshot

    def delayed(ctx: AppContext, session_id: str, snapshot: ConversationSnapshot) -> None:
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        original(ctx, session_id, snapshot)

    monkeypatch.setattr(AppContext, "_save_durable_conversation_snapshot", delayed)
    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    ctx.conversation_registry.max_states = 1
    ctx.durable.create_conversation("chat")
    old = ctx.conversation_for("chat")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(old.update_snapshot, ConversationSnapshot(mode="chat", goal="pending"))
            try:
                assert entered.wait(5)
                if action in {"forget", "recreate"}:
                    assert not ctx.conversation_coordinator.forget("chat")
                    assert not ctx.durable.has_conversation("chat")
                    if action == "recreate":
                        ctx.conversation_for("chat").update_snapshot(ConversationSnapshot(mode="chat", goal="fresh"))
                elif action == "evict":
                    ctx.conversation_for("other")
                    assert "chat" not in ctx.conversation_registry.contexts
            finally:
                release.set()
            pending.result(timeout=5)
        saved = ctx.conversation_coordinator.load_snapshot("chat")
        if action == "forget":
            assert not ctx.durable.has_conversation("chat"), "the old callback resurrected the deleted conversation"
        elif action == "evict":
            assert saved is None, "the evicted context persisted a late snapshot"
        else:
            assert saved is not None
            assert saved.goal == ("fresh" if action == "recreate" else "pending")
    finally:
        release.set()
        assert ctx.close()
