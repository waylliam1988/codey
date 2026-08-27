from __future__ import annotations

from dataclasses import replace
import json
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

import codey.ghost.affinity as affinity_module
import codey.ghost.work_queue as work_queue_module
from codey.ghost.affinity import AffinityEdge, AffinityNode, GhostAffinityStore
from codey.ghost.hebbian import GhostHebbianStore, GhostNode
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.schema import GhostSignal, GhostSignalParseResult
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.knowledge.research_interest import ResearchInterestCandidate


FRESH_TS = "2999-01-01T00:00:00Z"


def _signal(
    *,
    kind: str = "style_preference",
    scope: str = "user",
    summary: str = "Prefer answer-first replies.",
    quote: str = "以后先给结论",
    confidence: float = 0.9,
    conflict_key: str = "reply_structure",
    value_key: str = "answer_first",
) -> GhostSignal:
    return GhostSignal(
        kind=kind,
        scope=scope,
        summary=summary,
        evidence_quote=quote,
        confidence=confidence,
        metadata={"conflict_key": conflict_key, "value_key": value_key},
        source="test",
    )


def _accepted_candidate(
    inbox: GhostInboxStore,
    *,
    signal: GhostSignal | None = None,
    session_id: str = "s1",
    run_id: str = "r1",
    project: str = "",
):
    created = inbox.ingest_signals(
        GhostSignalParseResult(signals=(signal or _signal(),), ok=True, provider_id="test"),
        session_id=session_id,
        run_id=run_id,
        project=project,
        user_text=signal.evidence_quote if signal is not None else "以后先给结论",
    )
    assert len(created) == 1
    reviewed = inbox.review_candidate(created[0].id, "accept", reviewed_by="test")
    assert reviewed is not None
    return reviewed


def _candidate(
    *,
    candidate_id: str = "ric-1",
    question: str = "Research whether copper supply and helium supply are connected",
    concepts: tuple[str, ...] = ("copper supply", "helium supply"),
    neighbors: tuple[str, ...] = ("war",),
    session_id: str = "s1",
    project: str = "",
    priority: float = 0.7,
    confidence: float = 0.8,
) -> ResearchInterestCandidate:
    return ResearchInterestCandidate(
        id=candidate_id,
        question=question,
        related_concepts=concepts,
        shared_neighbors=neighbors,
        source_refs=("note:war-copper", "note:war-helium"),
        scope="session" if session_id else "project" if project else "user",
        scope_ref=session_id or project,
        priority=priority,
        confidence=confidence,
        why_now="Shared neighbor: war.",
        source="concept_open_question",
        source_ref="concept:pair",
        strong_support=True,
    )


def _work_item(
    *,
    item_id_seed: str,
    status: str,
    project: Path,
    kind: str = "coding",
    priority: float = 0.7,
    proof_refs: tuple[str, ...] = (),
):
    item = work_queue_module._new_item(
        kind=kind,
        status="queued" if status == "done" else status,
        scope="project",
        scope_ref=str(project),
        title=f"{kind} follow-up {item_id_seed}",
        why_now="Bounded local test item.",
        priority=priority,
        confidence=0.85,
        source="user",
        source_ref=f"seed-{item_id_seed}",
        evidence_refs=(f"evidence:{item_id_seed}",),
        run_refs=(),
        now=FRESH_TS,
        metadata={"related_concepts": ["provider recovery"]},
    )
    return replace(
        item,
        status=status,
        proof_refs=proof_refs,
        completed_run_id="run-done" if status == "done" else "",
        blocked_reason="missing_proof" if status == "blocked" else "",
    )


def _affinity_node(
    node_id: str,
    kind: str,
    key: str,
    scope: str,
    scope_ref: str,
    *,
    weight: float,
) -> AffinityNode:
    return AffinityNode(
        id=node_id,
        kind=kind,
        key=key,
        label=f"{kind}:{key}",
        scope=scope,
        scope_ref=scope_ref,
        status="active",
        weight=weight,
        confidence=0.9,
        source_refs=(f"node:{key}",),
        created_at=FRESH_TS,
        updated_at=FRESH_TS,
        last_reinforced_at=FRESH_TS,
    )


def _affinity_edge(
    source: str,
    target: str,
    relation: str,
    scope: str,
    scope_ref: str,
    *,
    weight: float,
    source_ref: str,
) -> AffinityEdge:
    return AffinityEdge(
        id=affinity_module._edge_id(source, target, relation, scope, scope_ref),
        source=source,
        target=target,
        relation=relation,
        scope=scope,
        scope_ref=scope_ref,
        status="active",
        weight=weight,
        confidence=0.9,
        source_refs=(source_ref,),
        created_at=FRESH_TS,
        updated_at=FRESH_TS,
        last_reinforced_at=FRESH_TS,
    )


def _write_work_snapshot(store: GhostWorkQueueStore, items) -> None:
    store.events_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "schema_version": work_queue_module.WORK_QUEUE_SCHEMA_VERSION,
        "type": "ghost_work_snapshot",
        "event_id": "test_work_snapshot",
        "ts": FRESH_TS,
        "reason": "test_seed",
        "items": [item.to_payload() for item in items],
    }
    store.events_path.write_text(
        json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert store.rebuild_from_events()


def _write_affinity_snapshot(store: GhostAffinityStore, nodes, edges) -> None:
    store.events_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "schema_version": affinity_module.AFFINITY_SCHEMA_VERSION,
        "type": "ghost_affinity_snapshot",
        "event_id": "test_affinity_snapshot",
        "ts": FRESH_TS,
        "reason": "test_seed",
        "nodes": [node.to_payload() for node in nodes],
        "edges": [edge.to_payload() for edge in edges],
    }
    store.events_path.write_text(
        json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert store.rebuild_from_events()


def test_hebbian_accepted_node_syncs_to_affinity_node_without_raw_text() -> None:
    with tempfile.TemporaryDirectory() as td:
        inbox = GhostInboxStore(td)
        candidate = _accepted_candidate(inbox)
        hebbian = GhostHebbianStore(td)
        assert hebbian.reinforce_candidate(candidate).applied
        affinity = GhostAffinityStore(td)

        result = affinity.sync_from_sources(hebbian_store=hebbian)
        nodes = affinity.list_nodes(kind="user_preference")
        raw = affinity.events_path.read_text(encoding="utf-8")

    assert result.ok
    assert len(nodes) == 1
    assert nodes[0].kind == "user_preference"
    assert nodes[0].metadata["source"] == "hebbian"
    assert nodes[0].metadata["hebbian_node_id"]
    assert "Prefer answer-first replies" not in raw


def test_inactive_hebbian_nodes_do_not_enter_affinity() -> None:
    inactive = GhostNode(
        id="ghn-old",
        kind="style_preference",
        label="Old preference",
        conflict_key="style_preference:reply_length",
        value_key="concise",
        status="superseded",
        scope="user",
        scope_ref="",
        weight=0.9,
        confidence=0.9,
        candidate_ids=("c1",),
        evidence_refs=("e1",),
        created_at=FRESH_TS,
        updated_at=FRESH_TS,
        last_reinforced_at=FRESH_TS,
    )
    fake_hebbian = mock.Mock(list_nodes=mock.Mock(return_value=(inactive,)))
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)

        result = affinity.sync_from_sources(hebbian_store=fake_hebbian)

    assert result.ok
    assert result.skipped_reason == "no_change"
    assert affinity.list_nodes() == ()


def test_source_ref_replay_is_noop() -> None:
    with tempfile.TemporaryDirectory() as td:
        inbox = GhostInboxStore(td)
        candidate = _accepted_candidate(inbox)
        hebbian = GhostHebbianStore(td)
        hebbian.reinforce_candidate(candidate)
        affinity = GhostAffinityStore(td)
        with mock.patch("codey.ghost.affinity._now", return_value=FRESH_TS):
            first = affinity.sync_from_sources(hebbian_store=hebbian)
            before = affinity.events_path.read_text(encoding="utf-8")

            second = affinity.sync_from_sources(hebbian_store=hebbian)
            after = affinity.events_path.read_text(encoding="utf-8")

    assert first.ok
    assert second.ok
    assert second.skipped_reason == "no_change"
    assert after == before
    assert "<generator object" not in after


def test_reference_count_does_not_scale_reinforcement() -> None:
    def node_spec(refs: tuple[str, ...]) -> affinity_module._NodeSpec:
        return affinity_module._NodeSpec(
            kind="task_type",
            key="research",
            label="research",
            scope="project",
            scope_ref="proj",
            confidence=0.9,
            reward=0.9,
            source_refs=refs,
        )

    one = affinity_module._reinforce_node(
        None, node_spec(("ref:1",)), now=FRESH_TS
    )[0]
    five = affinity_module._reinforce_node(
        None,
        node_spec(tuple(f"ref:{index}" for index in range(5))),
        now=FRESH_TS,
    )[0]

    # Provenance count must not scale the learning signal: 5 refs are the
    # same ONE reinforcement event as 1 ref.
    assert abs(one.weight - five.weight) < 1e-12

    def edge_spec(refs: tuple[str, ...]) -> affinity_module._EdgeSpec:
        return affinity_module._EdgeSpec(
            source="provider-a",
            target="task_research",
            relation="works_well_for",
            scope="project",
            scope_ref="proj",
            confidence=0.9,
            reward=0.9,
            source_refs=refs,
        )

    edge_one = affinity_module._reinforce_edge(
        None, edge_spec(("ref:1",)), now=FRESH_TS
    )[0]
    edge_five = affinity_module._reinforce_edge(
        None,
        edge_spec(tuple(f"ref:{index}" for index in range(5))),
        now=FRESH_TS,
    )[0]

    assert abs(edge_one.weight - edge_five.weight) < 1e-12
    assert len(five.source_refs) == 5
    assert len(edge_five.source_refs) == 5


def test_work_queue_done_and_blocked_generate_bounded_edges() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        queue = GhostWorkQueueStore(td)
        done = _work_item(item_id_seed="done", status="done", project=project, proof_refs=("diff:run-done",))
        blocked = _work_item(item_id_seed="blocked", status="blocked", project=project)
        _write_work_snapshot(queue, [done, blocked])
        affinity = GhostAffinityStore(td)

        result = affinity.sync_from_sources(work_queue_store=queue, project=str(project))
        relations = {edge.relation for edge in affinity.list_edges(project=str(project))}

    assert result.ok
    assert "works_well_for" in relations
    assert "struggles_with" in relations
    assert "used_in_task" in relations


def test_work_priority_hints_use_relevant_target_edge_among_irrelevant_edges() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        scope = "project"
        scope_ref = affinity_module._scope_ref("project", str(project))
        affinity = GhostAffinityStore(td)
        project_id = affinity_module._node_id("project", scope, scope_ref, scope_ref)
        task_id = affinity_module._node_id("task_type", scope, scope_ref, "research")
        nodes = [
            _affinity_node(project_id, "project", scope_ref, scope, scope_ref, weight=0.9),
            _affinity_node(task_id, "task_type", "research", scope, scope_ref, weight=0.05),
        ]
        edges = [
            _affinity_edge(project_id, task_id, "used_in_task", scope, scope_ref, weight=0.9, source_ref="edge:relevant"),
        ]
        for index in range(20):
            other_task_id = affinity_module._node_id("task_type", scope, scope_ref, f"other-{index}")
            nodes.append(_affinity_node(other_task_id, "task_type", f"other-{index}", scope, scope_ref, weight=0.05))
            edges.append(_affinity_edge(
                project_id,
                other_task_id,
                "used_in_task",
                scope,
                scope_ref,
                weight=0.2,
                source_ref=f"edge:other-{index}",
            ))
        _write_affinity_snapshot(affinity, nodes, edges)
        item = _work_item(item_id_seed="target", status="queued", project=project, kind="research")

        hints = affinity.query_work_priority_hints((item,), project=str(project))

    assert len(hints) == 1
    assert hints[0].target == item.id
    assert hints[0].reason_code == "associated_task_reinforced"
    assert "edge:relevant" in hints[0].source_refs


def test_research_concept_is_not_evidence() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)

        result = affinity.sync_from_sources(research_interest_candidates=(_candidate(),), session_id="s1")
        concepts = affinity.list_nodes(kind="research_concept", session_id="s1")
        edges = affinity.list_edges(relation="associated_with", session_id="s1")

    assert result.ok
    assert {node.key for node in concepts} >= {"copper supply", "helium supply"}
    assert all(node.evidence_refs == () for node in concepts)
    assert edges
    assert all(edge.proof_refs == () for edge in edges)


def test_scope_isolation_and_project_hints_do_not_leak() -> None:
    with tempfile.TemporaryDirectory() as td:
        project_one = str(Path(td, "one"))
        project_two = str(Path(td, "two"))
        affinity = GhostAffinityStore(td)
        affinity.sync_from_sources(
            research_interest_candidates=(
                _candidate(candidate_id="one", session_id="", project=project_one, concepts=("alpha", "beta"), neighbors=()),
                _candidate(candidate_id="two", session_id="", project=project_two, concepts=("gamma", "delta"), neighbors=()),
            ),
            project=project_one,
        )
        candidate_one = _candidate(candidate_id="target-one", session_id="", project=project_one, concepts=("alpha",), neighbors=())
        candidate_two = _candidate(
            candidate_id="target-two",
            session_id="",
            project=project_two,
            concepts=("alpha",),
            neighbors=(),
        )

        hints_one = affinity.query_research_priority_hints((candidate_one,), project=project_one)
        hints_two = affinity.query_research_priority_hints((candidate_two,), project=project_two)
        project_one_nodes = {node.key for node in affinity.list_nodes(kind="research_concept", project=project_one)}
        project_two_nodes = {node.key for node in affinity.list_nodes(kind="research_concept", project=project_two)}
        explicit_project_one_nodes = {
            node.key for node in affinity.list_nodes(kind="research_concept", scope="project", project=project_one)
        }

    assert [hint.target for hint in hints_one] == ["target-one"]
    assert hints_two == ()
    assert project_one_nodes == {"alpha", "beta"}
    assert project_two_nodes == {"gamma", "delta"}
    assert explicit_project_one_nodes == {"alpha", "beta"}


def test_provider_failure_does_not_store_raw_secret_message() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)

        result = affinity.sync_from_sources(
            terminal_event={
                "type": "task_done",
                "run_id": "run-secret",
                "session_id": "s1",
                "provider": "deepseek",
                "mode": "chat",
                "provider_failure": {
                    "provider": "deepseek",
                    "kind": "response_missing",
                    "action": "send",
                    "message": "raw sk-test-secret password should not persist",
                },
            },
            session_id="s1",
        )
        raw = affinity.events_path.read_text(encoding="utf-8")
        nodes = affinity.list_nodes(kind="provider_behavior", session_id="s1")

    assert result.ok
    assert nodes
    assert "response_missing" in raw
    assert "sk-test-secret" not in raw
    assert "password" not in raw


def test_event_write_failure_does_not_affect_projection_or_hints() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        with mock.patch.object(affinity, "_write_events_atomic", side_effect=OSError("event log down")):
            result = affinity.sync_from_sources(research_interest_candidates=(_candidate(),), session_id="s1")

        hints = affinity.query_research_priority_hints((_candidate(candidate_id="target"),), session_id="s1")

    assert not result.ok
    assert result.skipped_reason == "event_write_failed"
    assert not affinity.projection_path.exists()
    assert hints == ()


def test_unreadable_events_fail_closed_for_hints_but_allow_diagnostics_projection() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        affinity.sync_from_sources(research_interest_candidates=(_candidate(concepts=("alpha",)),), session_id="s1")
        assert affinity.query_research_priority_hints(
            (_candidate(candidate_id="target", concepts=("alpha",)),),
            session_id="s1",
        )

        affinity.events_path.write_bytes(b"\xff\xfe\xff")
        diagnostic_nodes = affinity.list_nodes(kind="research_concept", session_id="s1")
        hints = affinity.query_research_priority_hints(
            (_candidate(candidate_id="target", concepts=("alpha",)),),
            session_id="s1",
        )

    assert diagnostic_nodes
    assert hints == ()


def test_unreadable_or_oversized_events_block_mutating_sync() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        affinity.sync_from_sources(research_interest_candidates=(_candidate(),), session_id="s1")
        before = affinity.events_path.read_bytes()
        affinity.events_path.write_bytes(b"\xff\xfe\xff")

        result = affinity.sync_from_sources(
            research_interest_candidates=(_candidate(candidate_id="new", concepts=("new concept",)),),
            session_id="s1",
        )
        after = affinity.events_path.read_bytes()

    assert not result.ok
    assert result.skipped_reason == "events_read_blocked"
    assert "affinity_events_unreadable" in result.warnings
    assert after != before
    assert after == b"\xff\xfe\xff"


def test_missing_events_with_projection_blocks_mutating_sync() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        first = affinity.sync_from_sources(
            research_interest_candidates=(_candidate(candidate_id="alpha", concepts=("alpha",), neighbors=()),),
            session_id="s1",
        )
        affinity.events_path.unlink()

        diagnostic_nodes = affinity.list_nodes(kind="research_concept", session_id="s1")
        hints = affinity.query_research_priority_hints(
            (_candidate(candidate_id="target", concepts=("alpha",)),),
            session_id="s1",
        )
        second = affinity.sync_from_sources(
            research_interest_candidates=(_candidate(candidate_id="beta", concepts=("beta",), neighbors=()),),
            session_id="s1",
        )
        after_nodes = affinity.list_nodes(kind="research_concept", session_id="s1")

    assert first.ok
    assert {node.key for node in diagnostic_nodes} == {"alpha"}
    assert hints == ()
    assert not second.ok
    assert second.skipped_reason == "affinity_events_missing"
    assert {node.key for node in after_nodes} == {"alpha"}
    assert not affinity.events_path.exists()


def test_missing_events_projection_export_compact_and_delete_scope_are_diagnostic() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        affinity.sync_from_sources(
            research_interest_candidates=(_candidate(candidate_id="alpha", concepts=("alpha",), neighbors=()),),
            session_id="s1",
        )
        affinity.events_path.unlink()

        exported = affinity.export_state()
        compacted = affinity.compact_if_needed()
        with pytest.raises(OSError):
            affinity.delete_scope("session", session_id="s1")
        after_nodes = affinity.list_nodes(kind="research_concept", session_id="s1")

    assert exported["affinity"]["nodes"]
    assert exported["affinity"]["diagnostic"]["projection_only"] is True
    assert "affinity_events_missing" in exported["warnings"]
    assert not compacted["ok"]
    assert "affinity_events_missing" in compacted["warnings"]
    assert {node.key for node in after_nodes} == {"alpha"}
    assert not affinity.events_path.exists()


def test_reinforcement_weight_uses_only_new_refs() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        weights: list[float] = []
        with mock.patch("codey.ghost.affinity._now", return_value=FRESH_TS):
            for index in range(3):
                affinity.sync_from_sources(
                    research_interest_candidates=(
                        _candidate(candidate_id=f"alpha-{index}", concepts=("alpha",)),
                    ),
                    session_id="s1",
                )
                weights.append(affinity.list_nodes(kind="research_concept", session_id="s1")[0].weight)

    second_increment = round(weights[1] - weights[0], 4)
    third_increment = round(weights[2] - weights[1], 4)
    assert second_increment == third_increment
    assert weights[-1] < 1.0


def test_ref_cap_does_not_break_replay_idempotency() -> None:
    candidates = tuple(
        _candidate(candidate_id=f"alpha-{index}", concepts=("alpha",))
        for index in range(40)
    )
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        first = affinity.sync_from_sources(research_interest_candidates=candidates, session_id="s1")
        before = affinity.events_path.read_text(encoding="utf-8")

        second = affinity.sync_from_sources(research_interest_candidates=candidates, session_id="s1")
        after = affinity.events_path.read_text(encoding="utf-8")

    assert first.ok
    assert second.ok
    assert second.skipped_reason == "no_change"
    assert after == before


def test_projection_can_be_rebuilt_from_events() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        affinity.sync_from_sources(research_interest_candidates=(_candidate(),), session_id="s1")
        affinity.projection_path.write_text("{bad json", encoding="utf-8")

        assert affinity.rebuild_from_events()
        nodes = affinity.list_nodes(kind="research_concept", session_id="s1")

    assert nodes


def test_delete_scope_reset_and_export_cover_affinity() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        affinity.sync_from_sources(research_interest_candidates=(_candidate(),), session_id="s1")

        exported = affinity.export_state()
        removed = affinity.delete_scope("session", session_id="s1")
        after_delete = affinity.export_state()
        reset_ok = affinity.reset_all()

    assert exported["affinity"]["nodes"]
    assert removed["nodes"] > 0
    assert after_delete["affinity"]["nodes"] == []
    assert reset_ok
    assert not affinity.projection_path.exists()
    assert not affinity.events_path.exists()


def test_decay_preserves_last_reinforced_and_fanout_cap_prunes_edges() -> None:
    candidate = _candidate(concepts=("alpha", "beta", "gamma", "delta"))
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        with mock.patch.object(affinity_module, "MAX_EDGE_OUT_DEGREE", 1):
            with mock.patch("codey.ghost.affinity._now", return_value="2026-01-01T00:00:00Z"):
                affinity.sync_from_sources(research_interest_candidates=(candidate,), session_id="s1")
            before_nodes = affinity.list_nodes(session_id="s1")
            before_edges = affinity.list_edges(session_id="s1")
            with mock.patch("codey.ghost.affinity._now", return_value="2026-05-01T00:00:00Z"):
                result = affinity.decay()
            after = affinity.list_nodes(session_id="s1")[0]

    assert len(before_edges) <= 2
    assert result["decayed_nodes"] > 0
    assert after.last_reinforced_at == before_nodes[0].last_reinforced_at
    assert after.last_decayed_at == "2026-05-01T00:00:00Z"


def test_export_contains_valid_json_and_no_research_body() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        affinity.sync_from_sources(research_interest_candidates=(_candidate(),), session_id="s1")

        payload = affinity.export_state()
        raw = json.dumps(payload, ensure_ascii=False)

    assert payload["schema_version"] == 1
    assert "affinity_events" in payload
    assert "RAW SOURCE" not in raw
    assert "Research body" not in raw


def test_concurrent_reinforce_accumulates_both_events() -> None:
    with tempfile.TemporaryDirectory() as td:
        seed = GhostAffinityStore(td)
        with mock.patch("codey.ghost.affinity._now", return_value=FRESH_TS):
            assert seed.sync_from_sources(
                research_interest_candidates=(_candidate(candidate_id="alpha-base", concepts=("alpha",), neighbors=()),),
                session_id="s1",
            ).ok
            base_weight = seed.list_nodes(kind="research_concept", session_id="s1")[0].weight

            def reinforce(candidate_id: str) -> None:
                store = GhostAffinityStore(td)
                result = store.sync_from_sources(
                    research_interest_candidates=(_candidate(candidate_id=candidate_id, concepts=("alpha",), neighbors=()),),
                    session_id="s1",
                )
                assert result.ok

            threads = [
                threading.Thread(target=reinforce, args=("alpha-a",)),
                threading.Thread(target=reinforce, args=("alpha-b",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        final = GhostAffinityStore(td).list_nodes(kind="research_concept", session_id="s1")[0]

    expected_increment = affinity_module.NODE_LEARNING_RATE * 0.7 * 0.8
    assert round(final.weight - base_weight, 4) == round(expected_increment * 2, 4)
    assert {"research_interest:alpha-a", "research_interest:alpha-b"}.issubset(set(final.source_refs))


def test_snapshot_then_reinforce_continues_accumulating() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        with mock.patch("codey.ghost.affinity._now", return_value=FRESH_TS):
            assert affinity.sync_from_sources(
                research_interest_candidates=(_candidate(candidate_id="alpha-1", concepts=("alpha",), neighbors=()),),
                session_id="s1",
            ).ok
            before = affinity.list_nodes(kind="research_concept", session_id="s1")[0]
            with mock.patch.object(affinity_module, "MAX_AFFINITY_EVENTS", 0):
                compacted = affinity.compact_if_needed()
            assert compacted["compacted"] is True
            assert "ghost_affinity_snapshot" in affinity.events_path.read_text(encoding="utf-8")

            assert affinity.sync_from_sources(
                research_interest_candidates=(_candidate(candidate_id="alpha-2", concepts=("alpha",), neighbors=()),),
                session_id="s1",
            ).ok
            after = affinity.list_nodes(kind="research_concept", session_id="s1")[0]

    expected_increment = affinity_module.NODE_LEARNING_RATE * 0.7 * 0.8
    assert round(after.weight - before.weight, 4) == round(expected_increment, 4)


def test_old_affinity_upsert_event_is_unsupported_for_mutation() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        affinity.events_path.parent.mkdir(parents=True, exist_ok=True)
        affinity.events_path.write_text(
            json.dumps({
                "schema_version": affinity_module.AFFINITY_SCHEMA_VERSION,
                "type": "ghost_affinity_node_upsert",
                "event_id": "old",
                "ts": FRESH_TS,
                "node": {},
            }) + "\n",
            encoding="utf-8",
        )

        result = affinity.sync_from_sources(
            research_interest_candidates=(_candidate(candidate_id="alpha", concepts=("alpha",), neighbors=()),),
            session_id="s1",
        )

    assert not result.ok
    assert result.skipped_reason == "events_read_blocked"
    assert any("unsupported_event" in warning for warning in result.warnings)


def test_affinity_snapshot_with_invalid_row_is_unsupported_for_mutation() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        affinity.events_path.parent.mkdir(parents=True, exist_ok=True)
        affinity.events_path.write_text(
            json.dumps({
                "schema_version": affinity_module.AFFINITY_SCHEMA_VERSION,
                "type": "ghost_affinity_snapshot",
                "event_id": "bad-snapshot",
                "ts": FRESH_TS,
                "reason": "test",
                "nodes": [{"id": "missing-required-fields"}],
                "edges": [],
            }) + "\n",
            encoding="utf-8",
        )

        result = affinity.sync_from_sources(
            research_interest_candidates=(_candidate(candidate_id="alpha", concepts=("alpha",), neighbors=()),),
            session_id="s1",
        )

    assert not result.ok
    assert result.skipped_reason == "events_read_blocked"
    assert any("invalid_event" in warning for warning in result.warnings)


def test_affinity_mutation_result_propagates_projection_write_failure_warning() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        with mock.patch.object(affinity, "_write_projection", side_effect=OSError("disk full")):
            result = affinity.sync_from_sources(
                research_interest_candidates=(_candidate(candidate_id="alpha", concepts=("alpha",), neighbors=()),),
                session_id="s1",
            )

    assert result.ok
    assert "affinity_projection_write_failed" in result.warnings
    assert "affinity_projection_write_failed" in affinity.last_warnings


def test_affinity_delete_scope_propagates_projection_write_failure_warning() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        affinity.sync_from_sources(
            research_interest_candidates=(_candidate(candidate_id="alpha", concepts=("alpha",), neighbors=()),),
            session_id="s1",
        )
        with mock.patch.object(affinity, "_write_projection", side_effect=OSError("disk full")):
            result = affinity.delete_scope("session", session_id="s1")

    assert result["nodes"] > 0
    assert "affinity_projection_write_failed" in result["warnings"]
    assert "affinity_projection_write_failed" in affinity.last_warnings


def test_affinity_decay_propagates_projection_write_failure_warning() -> None:
    with tempfile.TemporaryDirectory() as td:
        affinity = GhostAffinityStore(td)
        with mock.patch("codey.ghost.affinity._now", return_value="2026-01-01T00:00:00Z"):
            affinity.sync_from_sources(
                research_interest_candidates=(_candidate(candidate_id="alpha", concepts=("alpha", "beta"), neighbors=()),),
                session_id="s1",
            )
        with mock.patch.object(affinity, "_write_projection", side_effect=OSError("disk full")):
            with mock.patch("codey.ghost.affinity._now", return_value="2026-05-01T00:00:00Z"):
                result = affinity.decay()

    assert result["decayed_nodes"] > 0
    assert "affinity_projection_write_failed" in result["warnings"]
    assert "affinity_projection_write_failed" in affinity.last_warnings


def test_affinity_schema_version_stays_cold_start_v1() -> None:
    assert affinity_module.AFFINITY_SCHEMA_VERSION == 1
