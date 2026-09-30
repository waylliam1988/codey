"""Ghost work queue is split by responsibility, not by file size.

- ``ghost.work_queue_model`` owns items, constants, and field rules.
- ``ghost.work_queue_events`` owns event construction/validation/pure replay.
- ``ghost.work_queue_sources`` owns continuity/research/checkpoint/run
  projection conversions.
- ``ghost.work_queue`` owns the store, locks, persistence, and transaction
  boundaries, and re-exports the split owners without compatibility shims
  for removed internals.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_work_queue_split_modules_exist() -> None:
    for name in ("work_queue_model", "work_queue_events", "work_queue_sources"):
        assert (ROOT / "codey/ghost" / f"{name}.py").exists(), name
    from codey.ghost import work_queue_events, work_queue_model, work_queue_sources

    assert hasattr(work_queue_model, "GhostWorkItem")
    assert callable(getattr(work_queue_events, "items_from_events", None)) or callable(
        getattr(work_queue_events, "replay_work_events", None)
    )
    assert callable(getattr(work_queue_sources, "items_from_continuity", None)) or hasattr(
        work_queue_sources, "items_from_continuity"
    )


def test_model_leaf_has_no_store_or_lock_dependency() -> None:
    import ast

    path = ROOT / "codey/ghost/work_queue_model.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert "codey.storage.file_lock" not in imports
    assert "codey.ghost.event_log" not in imports


def test_event_leaf_is_pure_replay() -> None:
    text = (ROOT / "codey/ghost/work_queue_events.py").read_text(encoding="utf-8-sig")
    assert "with_file_lock" not in text
    assert "write_json_atomic" not in text


def test_payload_round_trip_stays_stable() -> None:
    from codey.ghost.work_queue_model import GhostWorkItem

    item = GhostWorkItem(
        id="w1",
        kind="coding",
        status="queued",
        scope="project",
        scope_ref="proj",
        title="t",
        why_now="why",
        priority=0.5,
        confidence=0.5,
        source="continuity",
        source_ref="src",
    )
    restored = GhostWorkItem.from_payload(item.to_payload())
    assert restored is not None
    assert restored.id == "w1"
    assert restored.title == "t"
    assert restored.status == "queued"
