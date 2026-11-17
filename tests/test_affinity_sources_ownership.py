"""Affinity sources own hebbian/work/research/provider conversions.

Locks that accepted Hebbian nodes, work-item status mapping, research
candidates (never as read evidence), and provider outcomes (no sensitive
leak) produce real specs through ``collect_source_specs``, that the Store
goes through that owner, and that sources import neither Store nor events.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from codey.ghost.hebbian import GhostHebbianStore, GhostNode, _node_event
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.ghost.work_queue_events import _observed_event
from codey.ghost.work_queue_model import GhostWorkItem
from codey.knowledge.research_interest import ResearchInterestCandidate
from codey.runs.ledger_projection import ProviderFailureSummary, RunLedgerProjection

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


def test_hebbian_accepted_node_generates_real_spec(tmp_path: Path) -> None:
    from codey.ghost.affinity_sources import collect_source_specs

    node = GhostNode(
        id="h1", kind="research_interest", status="active", scope="user", scope_ref="",
        label="Research preference", conflict_key="c", value_key="v", confidence=0.8,
        weight=0.6, evidence_refs=("e1",), candidate_ids=(), created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z", last_reinforced_at="2026-01-01T00:00:00Z",
    )
    hebbian = GhostHebbianStore(tmp_path)
    with hebbian.graph.transaction():
        hebbian.graph.append([_node_event(node, action="upsert")])
        hebbian.graph.replace_rows([node.to_payload()], ())
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


def test_work_item_status_maps_to_reward(tmp_path: Path) -> None:
    from codey.ghost.affinity_sources import collect_source_specs

    item = GhostWorkItem(
        id="w1",
        kind="coding",
        status="done",
        scope="user",
        scope_ref="",
        title="t",
        why_now="Completed work",
        priority=0.8,
        source="user",
        source_ref="user:work-1",
        confidence=0.9,
        updated_at="2999-01-01T00:00:00Z",
        proof_refs=("diff:run-1",),
        completed_run_id="run-1",
    )
    queue = GhostWorkQueueStore(tmp_path)
    with queue.records.transaction():
        queue.records.append([_observed_event(item, ts="2026-01-01T00:00:00Z")])
        queue.records.replace_rows([item.to_payload()])
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

    candidate = ResearchInterestCandidate(
        id="ric-1",
        scope="user",
        scope_ref="",
        confidence=0.8,
        priority=0.7,
        source="concept_open_question",
        source_ref="concept:1",
        question="What needs checking?",
        why_now="An open question",
        related_concepts=("concept",),
        shared_neighbors=(),
        source_refs=(),
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
    assert all("not_evidence" in n.metadata for n in nodes)


def test_provider_outcome_hides_sensitive_text() -> None:
    from codey.ghost.affinity_sources import collect_source_specs

    run = RunLedgerProjection(
        run_id="run-1",
        mode="coding",
        provider_failures=(ProviderFailureSummary(
            provider="p", kind="timeout", action="send", stage="s", message="sk-secret-abc",
        ),),
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
        f"{n.key} {n.label} {n.source_refs} {dict(n.metadata)}"
        for n in nodes
    )
    assert "sk-secret-abc" not in blob


def test_store_sync_goes_through_collect_source_specs() -> None:
    import inspect

    from codey.ghost import affinity as store_module
    from codey.ghost import affinity_sources as sources

    store_members: Mapping[str, object] = vars(store_module)
    assert store_members["collect_source_specs"] is sources.collect_source_specs
    src = inspect.getsource(store_module.GhostAffinityStore.sync_from_sources)
    assert "collect_source_specs" in src


def test_sources_leaf_imports_neither_store_nor_events() -> None:
    imports = _imports_of(ROOT / "codey" / "ghost" / "affinity_sources.py")
    assert "codey.ghost.affinity" not in imports
    assert "codey.ghost.affinity_events" not in imports
