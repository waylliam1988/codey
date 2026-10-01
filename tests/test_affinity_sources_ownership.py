"""Affinity sources own hebbian/work/research/provider conversions.

Locks that accepted Hebbian nodes, work-item status mapping, research
candidates (never as read evidence), and provider outcomes (no sensitive
leak) produce real specs through ``collect_source_specs``, that the Store
goes through that owner, and that sources import neither Store nor events.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_hebbian_accepted_node_generates_real_spec() -> None:
    from codey.ghost.affinity_sources import collect_source_specs

    hebbian = SimpleNamespace(
        list_nodes=lambda status="": [
            SimpleNamespace(
                id="h1",
                kind="research_interest",
                status="active",
                superseded_by="",
                scope="user",
                scope_ref="",
                conflict_key="c",
                value_key="v",
                confidence=0.8,
                weight=0.6,
                evidence_refs=("e1",),
            )
        ]
    )
    nodes, _edges = collect_source_specs(
        hebbian_store=hebbian,
        work_queue_store=None,
        research_interest_candidates=[],
        run_projection=None,
        terminal_event=None,
        session_id="",
        project="",
    )
    assert any(n.key for n in nodes)


def test_work_item_status_maps_to_reward() -> None:
    from codey.ghost.affinity_sources import collect_source_specs

    item = SimpleNamespace(
        id="w1",
        kind="coding",
        status="done",
        scope="user",
        scope_ref="",
        title="t",
        confidence=0.9,
        updated_at="2999-01-01T00:00:00Z",
        proof_refs=("diff:run-1",),
        metadata={},
    )
    queue = SimpleNamespace(list_items=lambda: [item])
    nodes, edges = collect_source_specs(
        hebbian_store=None,
        work_queue_store=queue,
        research_interest_candidates=[],
        run_projection=None,
        terminal_event=None,
        session_id="",
        project="",
    )
    assert nodes, "done work item must produce a task_type node"


def test_research_candidate_is_not_read_evidence() -> None:
    from codey.ghost.affinity_sources import collect_source_specs

    candidate = SimpleNamespace(
        id="ric-1",
        scope="user",
        scope_ref="",
        confidence=0.8,
        priority=0.7,
        source="concept_open_question",
        related_concepts=["concept"],
        shared_neighbors=[],
        source_refs=[],
    )
    nodes, _edges = collect_source_specs(
        hebbian_store=None,
        work_queue_store=None,
        research_interest_candidates=[candidate],
        run_projection=None,
        terminal_event=None,
        session_id="",
        project="",
    )
    assert nodes
    assert all("not_evidence" in dict(getattr(n, "metadata", {}) or {}) for n in nodes)


def test_provider_outcome_hides_sensitive_text() -> None:
    from codey.ghost.affinity_sources import collect_source_specs

    run = SimpleNamespace(
        run_id="run-1",
        mode="coding",
        provider_failures=[
            {"provider": "p", "kind": "timeout", "action": "send", "stage": "s",
             "detail": "sk-secret-abc"},
        ],
    )
    nodes, _edges = collect_source_specs(
        hebbian_store=None,
        work_queue_store=None,
        research_interest_candidates=[],
        run_projection=run,
        terminal_event=None,
        session_id="s1",
        project="",
    )
    blob = " ".join(
        f"{n.key} {n.label} {n.source_refs} {dict(getattr(n, 'metadata', {}) or {})}"
        for n in nodes
    )
    assert "sk-secret-abc" not in blob


def test_store_sync_goes_through_collect_source_specs() -> None:
    import inspect

    from codey.ghost import affinity as store_module
    from codey.ghost import affinity_sources as sources

    assert store_module.collect_source_specs is sources.collect_source_specs
    src = inspect.getsource(store_module.GhostAffinityStore.sync_from_sources)
    assert "collect_source_specs" in src


def test_sources_leaf_imports_neither_store_nor_events() -> None:
    imports = _imports_of(ROOT / "codey" / "ghost" / "affinity_sources.py")
    assert "codey.ghost.affinity" not in imports
    assert "codey.ghost.affinity_events" not in imports
