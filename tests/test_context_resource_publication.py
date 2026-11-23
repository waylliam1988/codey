"""Lazy knowledge construction must respect a later disable or shutdown."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from codey.app import context
from codey.knowledge.store import KnowledgeStore


@pytest.mark.parametrize("action", ["close", "disable", "unchanged", "replace"])
def test_lazy_knowledge_publication_observes_the_current_resource_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    home = tmp_path / "state"
    monkeypatch.setattr(context, "DEFAULT_STATE_HOME", home)
    ctx = context.AppContext(home, start_scheduler=False, memory_enabled=False)
    ready, publish, discarded = Event(), Event(), Event()
    created: list[KnowledgeStore] = []
    construct = KnowledgeStore
    replacement: KnowledgeStore | None = None

    def delayed(root: Path) -> KnowledgeStore:
        store = construct(root)
        close = store.close

        def observed_close() -> None:
            close()
            discarded.set()

        monkeypatch.setattr(store, "close", observed_close)
        created.append(store)
        ready.set()
        assert publish.wait(5)
        return store

    monkeypatch.setattr(context, "KnowledgeStore", delayed)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(lambda: ctx.knowledge_store)
            try:
                assert ready.wait(5)
                if action == "close":
                    assert ctx.close()
                elif action == "disable":
                    ctx.knowledge_store = None
                elif action == "replace":
                    replacement = construct(tmp_path / "replacement")
                    ctx.knowledge_store = replacement
            finally:
                publish.set()
            result = pending.result(timeout=5)
        assert len(created) == 1
        if action in {"close", "disable"}:
            assert result is None, "an obsolete construction resurrected a disabled resource"
            assert ctx.knowledge_store is None
            assert discarded.is_set()
        elif action == "replace":
            assert result is replacement and ctx.knowledge_store is replacement
            assert discarded.is_set()
        else:
            assert result is created[0] and ctx.knowledge_store is created[0]
            assert not discarded.is_set()
    finally:
        publish.set()
        assert ctx.close()
        for store in created:
            if not discarded.is_set():
                store.close()
