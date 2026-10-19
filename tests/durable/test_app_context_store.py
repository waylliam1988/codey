from __future__ import annotations

from pathlib import Path

from codey.app.context import AppContext
from codey.durable import DurableSession


def test_app_context_owns_durable_store_and_reopens(tmp_path: Path) -> None:
    state = AppContext(tmp_path / "state")
    try:
        assert isinstance(state.durable, DurableSession)
        assert not hasattr(state, "transcript_store")
        state.durable.create_conversation("conversation-1")
        assert (tmp_path / "state" / "durable.sqlite3").exists()
    finally:
        assert state.close()

    restarted = AppContext(tmp_path / "state")
    try:
        assert restarted.durable.head("conversation-1") == (None, 0)
    finally:
        assert restarted.close()
