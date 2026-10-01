"""Affinity model owns data, identity, and payload constraints.

Locks that node/edge/hint/sync/spec types and their stable ids live in
``affinity_model`` with round-trip payloads, and that the model leaf pulls
no Store, lock, or I/O dependencies.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imports_of(path: Path) -> set[str]:
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_model_module_owns_types_and_identity() -> None:
    from codey.ghost import affinity_model

    for name in (
        "AffinityNode",
        "AffinityEdge",
        "AffinityHint",
        "GhostAffinitySyncResult",
        "AffinityNodeSpec",
        "AffinityEdgeSpec",
    ):
        assert hasattr(affinity_model, name), name
    assert callable(getattr(affinity_model, "_node_id", None))
    assert callable(getattr(affinity_model, "_edge_id", None))


def test_model_payload_round_trip_stable() -> None:
    from codey.ghost.affinity_model import AffinityEdge, AffinityNode

    node = AffinityNode(
        id="gan_test",
        kind="research_concept",
        key="concept",
        label="concept:concept",
        scope="user",
        scope_ref="",
        status="active",
        weight=0.5,
        confidence=0.8,
    )
    restored = AffinityNode.from_payload(node.to_payload())
    assert restored is not None
    assert restored.id == "gan_test"
    assert restored.key == "concept"
    edge = AffinityEdge(
        id="gae_test",
        source="gan_test",
        target="gan_other",
        relation="associated_with",
        scope="user",
        scope_ref="",
        status="active",
        weight=0.4,
        confidence=0.7,
    )
    restored_edge = AffinityEdge.from_payload(edge.to_payload())
    assert restored_edge is not None
    assert restored_edge.id == "gae_test"


def test_model_ids_stable() -> None:
    from codey.ghost import affinity_model

    first = affinity_model._node_id("research_concept", "user", "", "concept")
    second = affinity_model._node_id("research_concept", "user", "", "concept")
    assert first == second and first.startswith("gan_")
    edge_first = affinity_model._edge_id("a", "b", "associated_with", "user", "")
    edge_second = affinity_model._edge_id("b", "a", "associated_with", "user", "")
    assert edge_first == edge_second


def test_model_leaf_has_no_store_or_io_dependency() -> None:
    imports = _imports_of(ROOT / "codey" / "ghost" / "affinity_model.py")
    for banned in (
        "codey.ghost.affinity",
        "codey.ghost.affinity_sources",
        "codey.ghost.affinity_events",
        "codey.storage.file_lock",
        "codey.storage.local_store",
        "codey.ghost.event_log",
    ):
        assert banned not in imports, banned
    text = (ROOT / "codey" / "ghost" / "affinity_model.py").read_text(encoding="utf-8-sig")
    assert "with_file_lock" not in text
    assert "write_json_atomic" not in text
