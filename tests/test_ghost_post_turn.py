"""Real Ghost store failures remain visible without disclosing private text."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from codey.agents.handoff import ConversationContext, ConversationSnapshot
from codey.app.context import AppContext
from codey.ghost.continuity import GhostContinuityResult, GhostContinuityStore, KnowledgeReader
from codey.ghost.hebbian import GhostHebbianStore
from codey.operations.context import RunFrame
from codey.operations.ghost_post_turn import GhostTaskPolicyDeps, maybe_sync_continuity
from codey.runs.ledger_projection import RunLedgerProjection
from codey.task.model import TaskSubmission
from tests.test_ghost_received_input_lifetime import _queue


def test_continuity_failure_records_sanitized_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    events: list[dict[str, object]] = []

    def fail(self: GhostContinuityStore, *, hebbian_store: GhostHebbianStore | None = None,
             run_projection: RunLedgerProjection | None = None, knowledge_store: KnowledgeReader | None = None,
             user_focus_excerpt: str = "", session_id: str = "", run_id: str = "", project: str = "",
             mode: str = "", maintenance_as_of: str | None = None, maintenance: bool = False) -> GhostContinuityResult:
        calls.append(session_id)
        raise RuntimeError("secret path")

    def emit(event: dict[str, object]) -> None:
        events.append(dict(event))

    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        monkeypatch.setattr(GhostContinuityStore, "sync_from_sources", fail)
        monkeypatch.setattr(state, "emit", emit)
        request = TaskSubmission("session-1", None, "remember this", 4, False, "deepseek")
        frame = RunFrame(request, "run-1", "chat", None, "deepseek", "", ConversationContext(),
                         True, "", "", ConversationSnapshot("chat"), "", False, set(), 0)
        maybe_sync_continuity(GhostTaskPolicyDeps(state=state), frame, {
            "type": "task_done", "mode": "chat", "run_id": "run-1",
            "session_id": "session-1", "stop_reason": "done",
        })
    assert calls == ["session-1"]
    assert len(events) == 1
    warning = events[0]
    assert warning["type"] == "ghost_post_turn_warning"
    assert warning["stage"] == "continuity_sync"
    assert warning["run_id"] == "run-1" and warning["session_id"] == "session-1"
    assert warning["error_type"] == "RuntimeError"
    assert str(warning["error_ref"]).startswith("sha256:")
    assert "secret path" not in str(warning)


@pytest.mark.parametrize("stop_reason", ("approval", "question", "provider_failure", "cancelled"))
def test_suspended_or_failed_task_does_not_change_continuity(tmp_path: Path, stop_reason: str) -> None:
    from codey.ghost.continuity import build_ghost_continuity

    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        assert state.ghost_continuity is not None
        _queue(state).create("previous", {"session_id": "s", "project": None,
            "run_id": "previous", "task": "Prior completed request", "provider": "local", "intent": "chat"})
        state.ghost_continuity.sync_from_sources(user_focus_excerpt="Prior completed request",
            session_id="s", run_id="previous", mode="chat")
        before = build_ghost_continuity(state.ghost_continuity, session_id="s")
        request = TaskSubmission("s", None, "Search Fuzhou online", 4, False, "local")
        frame = RunFrame(request, "next", "chat", None, "local", "", ConversationContext(),
                         True, "", "", ConversationSnapshot("chat"), "", False, set(), 0)
        maybe_sync_continuity(GhostTaskPolicyDeps(state=state), frame, {
            "type": "task_done", "mode": "chat", "run_id": "next", "session_id": "s",
            "stop_reason": stop_reason,
        })
        after = build_ghost_continuity(state.ghost_continuity, session_id="s")
        assert after.text == before.text
        assert "Prior completed request" in after.text and "Search Fuzhou" not in after.text
