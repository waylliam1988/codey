from pathlib import Path

import pytest

from codey.durable import CommitConflict, DurableSession, MemoryDurableStore, SQLiteDurableStore


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_rejected_admission_never_leaves_an_empty_conversation(backend: str, tmp_path: Path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    try:
        session.admit_input(conversation_id="source", request_id="r", task_id="t", text="original", provider_snapshot={})
        sequence = session.commit_sequence()
        with pytest.raises(CommitConflict):
            session.admit_input(conversation_id="rejected", request_id="new", task_id="t", text="conflict", provider_snapshot={})
        assert not session.has_conversation("rejected")
        assert session.commit_sequence() == sequence
        assert session.task("t").conversation_id == "source"
    finally:
        session.close()
