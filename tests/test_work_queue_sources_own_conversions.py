"""Work-queue sources own continuity/checkpoint/run conversions.

Locks: continuity open questions convert without duplicates; checkpoint
and run projections convert with explicit signatures; the sources leaf
never imports the Store module.
"""
from __future__ import annotations

import inspect
from collections.abc import Iterable, Mapping
from pathlib import Path
from unittest.mock import patch

from codey.ghost.continuity import GhostContinuityItem, GhostContinuityStore, _item_event
from codey.ghost.work_queue_model import GhostWorkItem

ROOT = Path(__file__).resolve().parents[1]


def test_continuity_open_questions_convert_without_duplicates(tmp_path: Path) -> None:
    from codey.ghost.work_queue_sources import items_from_continuity

    item = GhostContinuityItem(
        kind="open_question", source="research_note", confidence=0.8,
        text="Why is the queue slow?", scope="session", scope_ref="s1",
        source_ref="note-1", id="c1", weight=0.8,
        created_at="2026-10-01T00:00:00Z", updated_at="2026-10-01T00:00:00Z",
        expires_at="2026-11-01T00:00:00Z",
    )
    store = GhostContinuityStore(tmp_path)
    with store.records.transaction():
        store.records.append([_item_event(item, action="upsert", ts=item.created_at)])
        store.records.replace_rows([item.to_payload()])
    first = items_from_continuity(store, session_id="s1", project="proj", now="2026-10-01T00:00:00Z")
    second = items_from_continuity(store, session_id="s1", project="proj", now="2026-10-01T00:00:00Z")
    assert first, "one open question must convert"
    assert [row.id for row in first] == [row.id for row in second]


def test_sources_entry_has_explicit_signature() -> None:
    from codey.ghost.work_queue_sources import items_from_continuity

    params = inspect.signature(items_from_continuity).parameters
    assert "args" not in params and "kwargs" not in params, params
    assert {"session_id", "project", "now"} <= set(params)


def test_sources_leaf_does_not_import_store() -> None:
    import ast

    path = ROOT / "codey" / "ghost" / "work_queue_sources.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(n.name in {"codey.ghost.work_queue", "codey.ghost.work_queue_events"} for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module not in {"codey.ghost.work_queue", "codey.ghost.work_queue_events"}


def test_store_delegates_to_source_owner() -> None:
    import inspect

    from codey.ghost.work_queue import GhostWorkQueueStore

    path = ROOT / "codey" / "ghost" / "work_queue.py"
    text = path.read_text(encoding="utf-8-sig")
    assert "work_queue_sources" in text
    assert "work_queue_events" in text
    assert "def _items_from_events" not in text, "Store must not keep its own replay copy"
    assert "def _items_from_continuity" not in text, "Store must not keep its own source copy"
    sync_src = inspect.getsource(GhostWorkQueueStore.sync_from_sources)
    assert "items_from_continuity" in sync_src
    assert "items_from_events" in sync_src


def test_store_delegates_to_both_owners_separately(tmp_path: Path) -> None:
    from codey.ghost import work_queue as store_module
    from codey.ghost import work_queue_events as events
    from codey.ghost import work_queue_sources as sources
    from codey.ghost.work_queue import GhostWorkQueueStore

    exports: Mapping[str, object] = vars(store_module)
    assert exports["items_from_events"] is events.items_from_events
    assert exports["items_from_continuity"] is sources.items_from_continuity
    calls: list[str] = []

    def replay(rows: Iterable[dict[str, object]]) -> list[GhostWorkItem]:
        calls.append("replay")
        return events.items_from_events(rows)

    def convert(
        continuity_store: GhostContinuityStore | None, *, session_id: str, project: str, now: str,
    ) -> list[GhostWorkItem]:
        calls.append("sources")
        return sources.items_from_continuity(continuity_store, session_id=session_id, project=project, now=now)

    store = GhostWorkQueueStore(tmp_path)
    with (
        patch.object(store_module, "items_from_events", replay),
        patch.object(store_module, "items_from_continuity", convert),
    ):
        result = store.sync_from_sources(session_id="s1", project="p1")
    assert result.ok
    assert "replay" in calls, "Store must replay through work_queue_events"
    assert "sources" in calls, "Store must convert through work_queue_sources"
