from __future__ import annotations

from pathlib import Path

import pytest

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_owner_reverse_index_survives_restart(backend: str, tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(path)
    session = DurableSession(store)
    session.admit_input(
        conversation_id="conversation", request_id="parent-request", task_id="parent",
        text="parent", provider_snapshot={},
    )
    session.admit_input(
        conversation_id="conversation", request_id="child-request", task_id="child",
        text="child", provider_snapshot={}, owner_task_id="parent",
    )
    assert [task.task_id for task in store.owned_dependents("parent")] == ["child"]
    session.close()
    if backend == "sqlite":
        session = DurableSession(SQLiteDurableStore(path))
        try:
            assert [task.task_id for task in session.store.owned_dependents("parent")] == ["child"]
        finally:
            session.close()

