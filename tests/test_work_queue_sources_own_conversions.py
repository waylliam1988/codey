"""Work-queue sources own continuity/checkpoint/run conversions.

Locks: continuity open questions convert without duplicates; checkpoint
and run projections convert with explicit signatures; the sources leaf
never imports the Store module.
"""
from __future__ import annotations

import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_continuity_open_questions_convert_without_duplicates():
    from types import SimpleNamespace

    from codey.ghost.work_queue_sources import items_from_continuity

    store = SimpleNamespace(
        list_items=lambda project="", session_id="": (
            SimpleNamespace(
                kind="open_question", source="research_note", confidence=0.8,
                text="Why is the queue slow?", scope="session", scope_ref="s1",
                source_ref="note-1", id="c1",
            ),
        ),
    )
    first = items_from_continuity(store, session_id="s1", project="proj", now="2026-10-01T00:00:00Z")
    second = items_from_continuity(store, session_id="s1", project="proj", now="2026-10-01T00:00:00Z")
    assert first, "one open question must convert"
    assert [row.id for row in first] == [row.id for row in second]


def test_sources_entry_has_explicit_signature():
    from codey.ghost.work_queue_sources import items_from_continuity

    params = inspect.signature(items_from_continuity).parameters
    assert "args" not in params and "kwargs" not in params, params
    assert {"session_id", "project", "now"} <= set(params)


def test_sources_leaf_does_not_import_store():
    import ast

    path = ROOT / "codey" / "ghost" / "work_queue_sources.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(n.name in {"codey.ghost.work_queue", "codey.ghost.work_queue_events"} for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module not in {"codey.ghost.work_queue", "codey.ghost.work_queue_events"}


def test_store_delegates_to_source_owner():
    path = ROOT / "codey" / "ghost" / "work_queue.py"
    text = path.read_text(encoding="utf-8-sig")
    assert "work_queue_sources" in text or "work_queue_events" in text
    assert "def _items_from_events" not in text, "Store must not keep its own replay copy"
    assert "def _items_from_continuity" not in text, "Store must not keep its own source copy"
