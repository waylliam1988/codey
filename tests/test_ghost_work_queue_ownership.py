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


def _imports_of(path) -> set[str]:
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _top_level_defs(path) -> set[str]:
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def test_work_queue_split_modules_exist() -> None:
    for name in ("work_queue_model", "work_queue_events", "work_queue_sources"):
        assert (ROOT / "codey/ghost" / f"{name}.py").exists(), name
    from codey.ghost import work_queue_events, work_queue_model, work_queue_sources

    assert hasattr(work_queue_model, "GhostWorkItem")
    assert callable(getattr(work_queue_events, "items_from_events", None))
    assert not hasattr(work_queue_events, "replay_work_events")
    assert callable(getattr(work_queue_sources, "items_from_continuity", None))
    assert callable(getattr(work_queue_sources, "new_item", None))


def test_split_leaves_do_not_import_the_store() -> None:
    for name in ("work_queue_model", "work_queue_events", "work_queue_sources"):
        imports = _imports_of(ROOT / "codey/ghost" / f"{name}.py")
        assert "codey.ghost.work_queue" not in imports, name


def test_store_owns_no_replay_or_source_definition() -> None:
    defs = _top_level_defs(ROOT / "codey/ghost/work_queue.py")
    for name in (
        "_items_from_events",
        "_snapshot_items",
        "_valid_work_event",
        "_valid_work_transition",
        "_apply_transition_event",
        "_merge_items",
        "_bounded_items",
        "_new_item",
        "_items_from_continuity",
        "_items_from_research_interest_candidates",
        "_items_from_work_checkpoint",
        "_items_from_run_projection",
        "_items_from_terminal_event",
        "_research_proof_ref",
    ):
        assert name not in defs, name
    text = (ROOT / "codey/ghost/work_queue.py").read_text(encoding="utf-8-sig")
    assert "work_queue_events" in text
    assert "work_queue_sources" in text


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
