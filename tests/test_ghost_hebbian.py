from __future__ import annotations

from dataclasses import replace
import json
import math
import tempfile
import unittest
from unittest import mock

from codey.ghost.hebbian import (
    EDGE_LEARNING_RATE,
    HEBBIAN_SCHEMA_VERSION,
    NODE_LEARNING_RATE,
    NODE_KINDS,
    GhostHebbianStore,
    GhostNode,
)
from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from codey.ghost.schema import GhostSignal, GhostSignalParseResult


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


def _result(*signals: GhostSignal) -> GhostSignalParseResult:
    return GhostSignalParseResult(signals=tuple(signals), ok=True, provider_id="test")


def _ingest_one(
    inbox: GhostInboxStore,
    signal: GhostSignal,
    *,
    session_id: str = "s1",
    run_id: str = "r1",
    project: str,
) -> GhostMemoryCandidate:
    created = inbox.ingest_signals(
        _result(signal),
        session_id=session_id,
        run_id=run_id,
        project=project,
    )
    assert len(created) == 1
    return created[0]


def _accept(inbox: GhostInboxStore, candidate: GhostMemoryCandidate) -> GhostMemoryCandidate:
    reviewed = inbox.review_candidate(candidate.id, "accept", reviewed_by="test")
    assert reviewed is not None
    return reviewed


class GhostHebbianStoreTests(unittest.TestCase):
    def test_accepted_candidate_creates_weighted_node(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            candidate = _ingest_one(inbox, _signal(confidence=0.8), project=td)
            candidate = inbox.review_candidate(candidate.id, "accept", reviewed_by="test")
            assert candidate is not None

            result = hebbian.reinforce_candidate(candidate)

            self.assertTrue(result.applied)
            self.assertEqual(result.reason, "reinforced")
            nodes = hebbian.list_nodes()
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0].kind, "style_preference")
            self.assertEqual(nodes[0].conflict_key, "style_preference:reply_structure")
            self.assertEqual(nodes[0].value_key, "answer_first")
            self.assertAlmostEqual(nodes[0].weight, NODE_LEARNING_RATE * 0.8)
            self.assertIn(candidate.id, nodes[0].candidate_ids)
            self.assertEqual(nodes[0].evidence_refs, candidate.evidence_refs)
            self.assertIn("ghost_hebbian_node_upsert", hebbian.events_path.read_text(encoding="utf-8"))

    def test_candidate_and_rejected_rows_are_not_reinforced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            pending = _ingest_one(
                inbox,
                _signal(
                    kind="correction",
                    summary="The correct provider is local.",
                    quote="正确是本地 provider",
                    confidence=0.95,
                    conflict_key="provider",
                    value_key="local",
                ),
                project=td,
            )
            rejected = _ingest_one(inbox, _signal(confidence=0.2), run_id="r2", project=td)

            self.assertEqual(hebbian.reinforce_candidate(pending).reason, "candidate_not_accepted")
            self.assertEqual(hebbian.reinforce_candidate(rejected).reason, "candidate_not_accepted")
            self.assertEqual(hebbian.list_nodes(), ())

    def test_duplicate_evidence_ref_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            candidate = _ingest_one(inbox, _signal(confidence=0.9), project=td)

            first = hebbian.reinforce_candidate(candidate)
            second = hebbian.reinforce_candidate(candidate)

            self.assertTrue(first.applied)
            self.assertFalse(second.applied)
            self.assertEqual(second.reason, "duplicate_evidence")
            self.assertEqual(len(hebbian.list_nodes()), 1)
            self.assertEqual(hebbian.list_nodes()[0].weight, first.node.weight)

    def test_new_evidence_refs_raise_weight_without_exceeding_one(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            candidate = _ingest_one(inbox, _signal(confidence=1.0), project=td)
            first = hebbian.reinforce_candidate(candidate)
            enriched = replace(
                candidate,
                evidence_refs=tuple(f"{candidate.id}:{index}" for index in range(1, 9)),
            )

            second = hebbian.reinforce_candidate(enriched)

            self.assertTrue(first.applied)
            self.assertTrue(second.applied)
            self.assertEqual(hebbian.list_nodes()[0].weight, 1.0)

    def test_decay_is_deterministic_and_preserves_last_reinforced_at(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            candidate = _ingest_one(inbox, _signal(confidence=0.9), project=td)

            with mock.patch("codey.ghost.hebbian._now", return_value="2026-01-01T00:00:00Z"):
                hebbian.reinforce_candidate(candidate)
            before = hebbian.list_nodes()[0]

            with mock.patch("codey.ghost.hebbian._now", return_value="2026-04-01T00:00:00Z"):
                hebbian.decay()
            after = hebbian.list_nodes()[0]

            self.assertAlmostEqual(after.weight, before.weight * 0.5)
            self.assertEqual(after.last_reinforced_at, before.last_reinforced_at)
            self.assertEqual(after.last_decayed_at, "2026-04-01T00:00:00Z")
            self.assertNotEqual(after.updated_at, before.updated_at)

    def test_decay_uses_continuous_half_life_curve(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            candidate = _ingest_one(inbox, _signal(confidence=0.9), project=td)

            with mock.patch("codey.ghost.hebbian._now", return_value="2026-01-01T00:00:00Z"):
                hebbian.reinforce_candidate(candidate)
            before = hebbian.list_nodes()[0]

            with mock.patch("codey.ghost.hebbian._now", return_value="2026-02-15T00:00:00Z"):
                hebbian.decay()
            after = hebbian.list_nodes()[0]

            expected = before.weight * math.exp(-math.log(2.0) * 0.5)
            self.assertAlmostEqual(after.weight, expected, places=6)

    def test_persisted_decay_is_idempotent_at_same_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            candidate = _ingest_one(inbox, _signal(confidence=0.9), project=td)

            with mock.patch("codey.ghost.hebbian._now", return_value="2026-01-01T00:00:00Z"):
                hebbian.reinforce_candidate(candidate)
            with mock.patch("codey.ghost.hebbian._now", return_value="2026-04-01T00:00:00Z"):
                hebbian.decay()
                first_decay = hebbian.list_nodes()[0]
                hebbian.decay()
                second_decay = hebbian.list_nodes()[0]

            self.assertEqual(second_decay.weight, first_decay.weight)
            self.assertEqual(second_decay.last_reinforced_at, first_decay.last_reinforced_at)
            self.assertEqual(second_decay.last_decayed_at, first_decay.last_decayed_at)

    def test_same_conflict_different_values_are_competing_nodes_until_manual_accept(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            first = _ingest_one(
                inbox,
                _signal(
                    summary="Prefer concise replies.",
                    quote="以后回答短一点",
                    conflict_key="reply_length",
                    value_key="concise",
                ),
                run_id="r1",
                project=td,
            )
            second = _ingest_one(
                inbox,
                _signal(
                    summary="Prefer detailed replies.",
                    quote="以后展开细节",
                    conflict_key="reply_length",
                    value_key="detailed",
                ),
                run_id="r2",
                project=td,
            )

            hebbian.reinforce_candidate(first)
            hebbian.reinforce_candidate(second)
            nodes = hebbian.list_nodes(status="active")

            self.assertEqual({node.value_key for node in nodes}, {"concise", "detailed"})
            self.assertEqual({node.status for node in nodes}, {"active"})

    def test_manual_accept_new_value_supersedes_old_active_node(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            first = _ingest_one(
                inbox,
                _signal(
                    summary="Prefer concise replies.",
                    quote="以后回答短一点",
                    conflict_key="reply_length",
                    value_key="concise",
                ),
                run_id="r1",
                project=td,
            )
            second = _ingest_one(
                inbox,
                _signal(
                    summary="Prefer detailed replies.",
                    quote="以后展开细节",
                    confidence=0.6,
                    conflict_key="reply_length",
                    value_key="detailed",
                ),
                run_id="r2",
                project=td,
            )
            hebbian.reinforce_candidate(first)
            reviewed = inbox.review_candidate(second.id, "accept", reviewed_by="test")
            assert reviewed is not None

            result = hebbian.reinforce_candidate(reviewed)
            nodes = {node.value_key: node for node in hebbian.list_nodes()}

            self.assertTrue(result.applied)
            self.assertEqual(nodes["detailed"].status, "active")
            self.assertEqual(nodes["concise"].status, "superseded")
            self.assertEqual(nodes["concise"].superseded_by, nodes["detailed"].id)

    def test_remove_candidate_deletes_node_and_connected_edges_from_active_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            first = _ingest_one(inbox, _signal(value_key="answer_first"), run_id="r1", project=td)
            second = _ingest_one(
                inbox,
                _signal(
                    summary="Prefer concise replies.",
                    quote="以后回答短一点",
                    conflict_key="reply_length",
                    value_key="concise",
                ),
                run_id="r1",
                project=td,
            )
            hebbian.reinforce_candidate(first)
            hebbian.reinforce_candidate(second, related_candidates=(first,))

            removed = hebbian.remove_candidate(second)

            self.assertEqual(removed, {"nodes": 1, "edges": 1})
            self.assertEqual(len(hebbian.list_nodes()), 1)
            self.assertEqual(hebbian.list_edges(), ())
            self.assertNotIn("Prefer concise replies.", hebbian.events_path.read_text(encoding="utf-8"))

    def test_duplicate_evidence_can_backfill_missing_coactivation_edge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            first = _ingest_one(inbox, _signal(value_key="answer_first"), run_id="r1", project=td)
            second = _ingest_one(
                inbox,
                _signal(
                    summary="Prefer concise replies.",
                    quote="以后回答短一点",
                    conflict_key="reply_length",
                    value_key="concise",
                ),
                run_id="r1",
                project=td,
            )
            hebbian.reinforce_candidate(first)
            hebbian.reinforce_candidate(second)

            result = hebbian.reinforce_candidate(second, related_candidates=(first,))

            self.assertTrue(result.applied)
            self.assertEqual(result.reason, "backfilled_edges")
            self.assertEqual(len(result.edges), 1)
            self.assertEqual(len(hebbian.list_edges()), 1)

    def test_sync_from_inbox_removes_rejected_candidate_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            candidate = _ingest_one(inbox, _signal(confidence=0.6), project=td)
            reviewed = inbox.review_candidate(candidate.id, "accept", reviewed_by="test")
            assert reviewed is not None
            hebbian.reinforce_candidate(reviewed)
            rejected = inbox.review_candidate(candidate.id, "reject", reviewed_by="test")
            assert rejected is not None

            results = hebbian.sync_from_inbox(inbox)

            self.assertTrue(any(result.reason == "removed_rejected_candidate" for result in results))
            self.assertEqual(hebbian.list_nodes(status="active"), ())

    def test_sync_from_inbox_backfills_each_coactivation_pair_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            first = _ingest_one(
                inbox,
                _signal(confidence=1.0, value_key="answer_first"),
                run_id="r1",
                project=td,
            )
            second = _ingest_one(
                inbox,
                _signal(
                    summary="Prefer concise replies.",
                    quote="以后回答短一点",
                    confidence=1.0,
                    conflict_key="reply_length",
                    value_key="concise",
                ),
                run_id="r1",
                project=td,
            )
            hebbian.reinforce_candidate(first)
            hebbian.reinforce_candidate(second)

            hebbian.sync_from_inbox(inbox)
            first_edges = hebbian.list_edges()
            hebbian.sync_from_inbox(inbox)
            second_edges = hebbian.list_edges()

            self.assertEqual(len(first_edges), 1)
            self.assertEqual(len(second_edges), 1)
            self.assertEqual(first_edges[0].weight, EDGE_LEARNING_RATE)
            self.assertEqual(second_edges[0].weight, EDGE_LEARNING_RATE)

    def test_project_scope_isolated_for_queries_and_delete_scope(self) -> None:
        with (
            tempfile.TemporaryDirectory() as state_td,
            tempfile.TemporaryDirectory() as project_a,
            tempfile.TemporaryDirectory() as project_b,
        ):
            inbox = GhostInboxStore(state_td)
            hebbian = GhostHebbianStore(state_td)
            node_a = _ingest_one(
                inbox,
                _signal(
                    scope="project",
                    summary="Project A memory unique phrase",
                    quote="这个项目记住 project A",
                    conflict_key="project_focus",
                    value_key="project_a",
                ),
                run_id="ra",
                project=project_a,
            )
            node_b = _ingest_one(
                inbox,
                _signal(
                    scope="project",
                    summary="Project B memory",
                    quote="这个项目记住 project B",
                    conflict_key="project_focus",
                    value_key="project_b",
                ),
                run_id="rb",
                project=project_b,
            )
            hebbian.reinforce_candidate(_accept(inbox, node_a))
            hebbian.reinforce_candidate(_accept(inbox, node_b))

            self.assertEqual(len(hebbian.list_nodes(scope="project", project=project_a)), 1)
            self.assertEqual(len(hebbian.list_nodes(scope="project", project=project_b)), 1)
            removed = hebbian.delete_scope("project", project=project_a)

            self.assertEqual(removed["nodes"], 1)
            self.assertEqual(hebbian.list_nodes(scope="project", project=project_a), ())
            self.assertEqual(len(hebbian.list_nodes(scope="project", project=project_b)), 1)
            self.assertNotIn("Project A memory unique phrase", hebbian.events_path.read_text(encoding="utf-8"))

    def test_coactivation_edges_only_use_same_run_accepted_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            first = _ingest_one(inbox, _signal(value_key="answer_first"), run_id="r1", project=td)
            second = _ingest_one(
                inbox,
                _signal(
                    summary="Prefer concise replies.",
                    quote="以后回答短一点",
                    conflict_key="reply_length",
                    value_key="concise",
                ),
                run_id="r1",
                project=td,
            )
            other_run = _ingest_one(
                inbox,
                _signal(
                    summary="Prefer markdown.",
                    quote="以后用 markdown",
                    conflict_key="format",
                    value_key="markdown",
                ),
                run_id="r2",
                project=td,
            )
            hebbian.reinforce_candidate(first)

            result = hebbian.reinforce_candidate(second, related_candidates=(first, other_run))

            self.assertTrue(result.applied)
            edges = hebbian.list_edges()
            self.assertEqual(len(edges), 1)
            self.assertEqual(edges[0].relation, "coactivated_with")
            self.assertAlmostEqual(edges[0].weight, EDGE_LEARNING_RATE * 0.9)

    def test_edge_fanout_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            related = [
                _ingest_one(
                    inbox,
                    _signal(
                        summary=f"Related memory {index}",
                        quote=f"以后记住 related {index}",
                        conflict_key=f"related_{index}",
                        value_key=f"related_{index}",
                    ),
                    run_id="r1",
                    project=td,
                )
                for index in range(3)
            ]
            for candidate in related:
                hebbian.reinforce_candidate(_accept(inbox, candidate))
            main = _ingest_one(
                inbox,
                _signal(
                    summary="Main memory",
                    quote="以后记住 main",
                    conflict_key="main",
                    value_key="main",
                ),
                run_id="r1",
                project=td,
            )

            with mock.patch("codey.ghost.hebbian.MAX_EDGE_OUT_DEGREE", 1):
                hebbian.reinforce_candidate(
                    _accept(inbox, main),
                    related_candidates=tuple(_accept(inbox, item) for item in related),
                )

            self.assertEqual(len(hebbian.list_edges()), 1)

    def test_bad_projection_is_quarantined_and_rebuilt_from_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            candidate = _ingest_one(inbox, _signal(), project=td)
            hebbian.reinforce_candidate(candidate)
            hebbian.state_path.write_text("{bad json", encoding="utf-8")

            nodes = hebbian.list_nodes()

            self.assertEqual(len(nodes), 1)
            self.assertTrue(list(hebbian.directory.glob("state.json.quarantine.*")))

    def test_bad_hebbian_event_line_is_skipped_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            candidate = _ingest_one(inbox, _signal(), project=td)
            hebbian.reinforce_candidate(candidate)
            with hebbian.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("{bad json\n")
            hebbian.state_path.unlink()

            nodes = hebbian.list_nodes()

            self.assertEqual(len(nodes), 1)
            self.assertTrue(any("bad_json" in item for item in hebbian.last_warnings))

    def test_projection_write_failure_is_fail_open_and_rebuildable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            candidate = _ingest_one(inbox, _signal(), project=td)

            with mock.patch("codey.ghost.hebbian.write_json_atomic", side_effect=OSError("disk full")):
                result = hebbian.reinforce_candidate(candidate)

            self.assertTrue(result.applied)
            self.assertFalse(hebbian.state_path.exists())
            self.assertEqual(len(hebbian.list_nodes()), 1)

    def test_oversize_events_block_ingest_without_overwriting_projection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            old_candidate = _ingest_one(inbox, _signal(value_key="old"), run_id="r1", project=td)
            hebbian.reinforce_candidate(_accept(inbox, old_candidate))
            old_events = hebbian.events_path.read_text(encoding="utf-8")
            hebbian.state_path.unlink()
            new_candidate = _ingest_one(
                inbox,
                _signal(summary="New memory", quote="以后记住 new", value_key="new"),
                run_id="r2",
                project=td,
            )

            with mock.patch("codey.ghost.hebbian.MAX_HEBBIAN_EVENTS_BYTES", 1):
                result = hebbian.reinforce_candidate(_accept(inbox, new_candidate))

            self.assertFalse(result.applied)
            self.assertEqual(result.reason, "events_read_blocked")
            self.assertEqual(hebbian.last_warnings, ("hebbian_events_too_large",))
            self.assertFalse(hebbian.state_path.exists())
            self.assertEqual(hebbian.events_path.read_text(encoding="utf-8"), old_events)

    def test_rebuild_reset_and_export_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            candidate = _ingest_one(inbox, _signal(), project=td)
            hebbian.reinforce_candidate(candidate)
            hebbian.state_path.unlink()

            self.assertTrue(hebbian.rebuild_from_events())
            exported = hebbian.export_state()
            self.assertEqual(len(exported["state"]["nodes"]), 1)
            self.assertEqual(len(exported["events"]), 1)
            self.assertTrue(hebbian.reset_all())
            self.assertFalse(hebbian.state_path.exists())
            self.assertFalse(hebbian.events_path.exists())

    def test_export_skips_bad_event_line_but_keeps_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            candidate = _ingest_one(inbox, _signal(), project=td)
            hebbian.reinforce_candidate(candidate)
            with hebbian.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps({"schema_version": 999}) + "\n")

            exported = hebbian.export_state()

            self.assertEqual(len(exported["state"]["nodes"]), 1)
            self.assertTrue(exported["warnings"])

    def test_future_node_kinds_are_not_loaded_before_extractor_support_exists(self) -> None:
        self.assertNotIn("boundary_preference", NODE_KINDS)
        payload = {
            "id": "future-node",
            "kind": "boundary_preference",
            "label": "Future boundary",
            "conflict_key": "boundary:future",
            "value_key": "future",
            "status": "active",
            "scope": "user",
            "scope_ref": "",
            "weight": 0.5,
            "confidence": 0.9,
            "candidate_ids": ["c1"],
            "evidence_refs": ["c1:1"],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "last_reinforced_at": "2026-01-01T00:00:00Z",
            "schema_version": HEBBIAN_SCHEMA_VERSION,
        }

        self.assertIsNone(GhostNode.from_payload(payload))


if __name__ == "__main__":
    unittest.main()
