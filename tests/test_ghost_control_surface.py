from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.schema import GhostSignal, GhostSignalParseResult
from codey.ghost.store import GhostSignalStore
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.work_checkpoint import WorkCheckpointStore


def _signal(
    *,
    kind: str = "style_preference",
    scope: str = "user",
    summary: str = "Prefer answer-first replies.",
    quote: str = "以后先给结论",
    confidence: float = 0.9,
) -> GhostSignal:
    return GhostSignal(
        kind=kind,
        scope=scope,
        summary=summary,
        evidence_quote=quote,
        confidence=confidence,
        metadata={"conflict_key": "reply_structure", "value_key": "answer_first"},
        source="test",
    )


def _ingest_candidate(
    state_home: str,
    *,
    signal: GhostSignal | None = None,
    session_id: str = "s1",
    run_id: str = "r1",
    project: str = "",
):
    inbox = GhostInboxStore(state_home)
    created = inbox.ingest_signals(
        GhostSignalParseResult(signals=(signal or _signal(),), ok=True, provider_id="test"),
        session_id=session_id,
        run_id=run_id,
        project=project,
        user_text=(signal or _signal()).evidence_quote,
    )
    assert len(created) == 1
    return created[0]


class GhostControlSurfaceTests(unittest.TestCase):
    def test_state_home_none_is_unavailable_and_actions_do_not_write(self) -> None:
        surface = GhostControlSurface.from_state_home(None)

        summary = surface.summary(session_id="s1", project="E:/project")
        status, action = surface.dispatch_action({"action": "disable_updates"})
        export = surface.export_state()

        self.assertEqual(status, 200)
        self.assertFalse(summary["ok"])
        self.assertFalse(summary["available"])
        self.assertEqual(summary["reason"], "unavailable")
        self.assertFalse(action["ok"])
        self.assertFalse(export["ok"])

    def test_summary_is_bounded_and_does_not_return_raw_candidate_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            quote = " ".join(["bounded evidence preview"] * 20)
            _ingest_candidate(td, signal=_signal(kind="correction", quote=quote, confidence=0.8))

            payload = GhostControlSurface.from_state_home(td).summary(session_id="s1")

        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["counts"]["review"], 1)
        self.assertNotIn("evidence_quote", encoded)
        self.assertNotIn("raw_text", encoded)
        self.assertLessEqual(len(payload["review"][0]["evidence_preview"]), 120)

    def test_accept_and_reject_candidate_update_inbox_and_hebbian_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            candidate = _ingest_candidate(td)
            surface = GhostControlSurface.from_state_home(td)

            accept_status, accept_payload = surface.dispatch_action({
                "action": "accept_candidate",
                "id": candidate.id,
            })
            reject_status, reject_payload = surface.dispatch_action({
                "action": "reject_candidate",
                "id": candidate.id,
            })

            inbox_rows = GhostInboxStore(td).list_candidates()
            active_nodes = GhostHebbianStore(td).list_nodes(status="active")

        self.assertEqual(accept_status, 200)
        self.assertTrue(accept_payload["ok"])
        self.assertTrue(accept_payload["state_update"]["applied"])
        self.assertEqual(reject_status, 200)
        self.assertTrue(reject_payload["ok"])
        self.assertEqual(inbox_rows[0].status, "rejected")
        self.assertEqual(active_nodes, ())

    def test_candidate_action_rejects_stale_project_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            other = Path(td, "other")
            project.mkdir()
            other.mkdir()
            candidate = _ingest_candidate(
                td,
                signal=_signal(kind="correction", scope="project", confidence=0.8),
                project=str(project),
            )
            surface = GhostControlSurface.from_state_home(td)

            stale_status, stale_payload = surface.dispatch_action({
                "action": "accept_candidate",
                "id": candidate.id,
                "project": str(other),
                "session_id": "s1",
            })
            missing_status, missing_payload = surface.dispatch_action({
                "action": "accept_candidate",
                "id": candidate.id,
            })
            current_status = GhostInboxStore(td).list_candidates()[0].status
            ok_status, ok_payload = surface.dispatch_action({
                "action": "accept_candidate",
                "id": candidate.id,
                "project": str(project),
                "session_id": "s1",
            })

        self.assertEqual(stale_status, 409)
        self.assertFalse(stale_payload["ok"])
        self.assertEqual(missing_status, 409)
        self.assertFalse(missing_payload["ok"])
        self.assertEqual(current_status, "candidate")
        self.assertEqual(ok_status, 200)
        self.assertTrue(ok_payload["ok"])

    def test_work_item_queue_and_reject_actions_use_store_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            project.mkdir()
            checkpoints = WorkCheckpointStore(td)
            checkpoint = checkpoints.start(
                run_id="run-old",
                session_id="s1",
                project=project,
                task="Finish the parser follow-up",
            )
            checkpoints.set_status(checkpoint, "interrupted", "error")
            queue = GhostWorkQueueStore(td)
            self.assertTrue(queue.sync_from_sources(
                work_checkpoint_store=checkpoints,
                session_id="s1",
                project=str(project),
            ).ok)
            item = queue.list_items(session_id="s1")[0]
            surface = GhostControlSurface.from_state_home(td)

            reject_status, reject_payload = surface.dispatch_action({
                "action": "reject_work_item",
                "id": item.id,
                "session_id": "s1",
                "project": str(project),
            })
            queue_status, queue_payload = surface.dispatch_action({
                "action": "queue_work_item",
                "id": item.id,
                "session_id": "s1",
                "project": str(project),
            })
            final = GhostWorkQueueStore(td).list_items(session_id="s1")[0]

        self.assertEqual(reject_status, 200)
        self.assertTrue(reject_payload["ok"])
        self.assertEqual(queue_status, 200)
        self.assertTrue(queue_payload["ok"])
        self.assertEqual(final.status, "queued")

    def test_work_item_action_rejects_stale_session_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            project.mkdir()
            checkpoints = WorkCheckpointStore(td)
            checkpoint = checkpoints.start(
                run_id="run-old",
                session_id="s1",
                project=project,
                task="Finish the parser follow-up",
            )
            checkpoints.set_status(checkpoint, "interrupted", "error")
            queue = GhostWorkQueueStore(td)
            self.assertTrue(queue.sync_from_sources(
                work_checkpoint_store=checkpoints,
                session_id="s1",
                project=str(project),
            ).ok)
            item = queue.list_items(session_id="s1")[0]
            surface = GhostControlSurface.from_state_home(td)

            stale_status, stale_payload = surface.dispatch_action({
                "action": "reject_work_item",
                "id": item.id,
                "session_id": "other",
                "project": str(project),
            })
            missing_status, missing_payload = surface.dispatch_action({
                "action": "reject_work_item",
                "id": item.id,
            })
            current_status = GhostWorkQueueStore(td).list_items(session_id="s1")[0].status
            ok_status, ok_payload = surface.dispatch_action({
                "action": "reject_work_item",
                "id": item.id,
                "session_id": "s1",
                "project": str(project),
            })

        self.assertEqual(stale_status, 409)
        self.assertFalse(stale_payload["ok"])
        self.assertEqual(missing_status, 409)
        self.assertFalse(missing_payload["ok"])
        self.assertEqual(current_status, "queued")
        self.assertEqual(ok_status, 200)
        self.assertTrue(ok_payload["ok"])

    def test_running_work_item_cannot_be_rejected_from_control_surface(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            project.mkdir()
            checkpoints = WorkCheckpointStore(td)
            checkpoint = checkpoints.start(
                run_id="run-old",
                session_id="s1",
                project=project,
                task="Finish the parser follow-up",
            )
            checkpoints.set_status(checkpoint, "interrupted", "error")
            queue = GhostWorkQueueStore(td)
            self.assertTrue(queue.sync_from_sources(
                work_checkpoint_store=checkpoints,
                session_id="s1",
                project=str(project),
            ).ok)
            claimed = queue.claim_next(
                session_id="s1",
                project=str(project),
                run_id="run-new",
                user_request="continue",
            )
            assert claimed.item is not None

            status, payload = GhostControlSurface.from_state_home(td).dispatch_action({
                "action": "reject_work_item",
                "id": claimed.item.id,
                "session_id": "s1",
                "project": str(project),
            })
            current = GhostWorkQueueStore(td).list_items(session_id="s1")[0]

        self.assertEqual(status, 409)
        self.assertFalse(payload["ok"])
        self.assertEqual(current.status, "running")

    def test_summary_association_health_is_scoped_to_current_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = Path(td, "first")
            second = Path(td, "second")
            first.mkdir()
            second.mkdir()
            inbox = GhostInboxStore(td)
            hebbian = GhostHebbianStore(td)
            for project, summary in ((first, "Use first project naming."), (second, "Use second project naming.")):
                candidate = _ingest_candidate(
                    td,
                    signal=_signal(
                        kind="correction",
                        scope="project",
                        summary=summary,
                        quote=summary,
                        confidence=0.9,
                    ),
                    run_id=summary,
                    project=str(project),
                )
                accepted = inbox.review_candidate(candidate.id, "accept", reviewed_by="test")
                assert accepted is not None
                self.assertTrue(hebbian.reinforce_candidate(accepted).applied)
            affinity = GhostAffinityStore(td)
            self.assertTrue(affinity.sync_from_sources(
                hebbian_store=hebbian,
                session_id="s1",
                project=str(first),
            ).ok)
            self.assertTrue(affinity.sync_from_sources(
                hebbian_store=hebbian,
                session_id="s1",
                project=str(second),
            ).ok)

            first_health = GhostControlSurface.from_state_home(td).summary(
                session_id="s1",
                project=str(first),
            )["health"]
            second_health = GhostControlSurface.from_state_home(td).summary(
                session_id="s1",
                project=str(second),
            )["health"]

        self.assertEqual(first_health["association_nodes"], 1)
        self.assertEqual(second_health["association_nodes"], 1)
        self.assertEqual(first_health["association_edges"], 0)

    def test_summary_maps_internal_store_warnings_to_neutral_ui_text(self) -> None:
        class WarningAffinity:
            last_warnings = ("affinity_events_missing",)

            def list_nodes(self, **_kwargs):
                return ()

            def list_edges(self, **_kwargs):
                return ()

        with tempfile.TemporaryDirectory() as td:
            payload = GhostControlSurface(
                inbox=GhostInboxStore(td),
                affinity=WarningAffinity(),
            ).summary(session_id="s1", project="E:/project")

        encoded = json.dumps(payload, ensure_ascii=False).lower()
        self.assertIn("Some local ordering could not be read", payload["health"]["warnings"])
        self.assertNotIn("affinity", encoded)
        self.assertNotIn("hebbian", encoded)
        self.assertNotIn("directive", encoded)

    def test_enable_disable_updates_and_confirmed_delete_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            project.mkdir()
            signal_store = GhostSignalStore(td)
            signal_store.append_extraction(
                GhostSignalParseResult(signals=(_signal(scope="project"),), ok=True, provider_id="test"),
                session_id="s1",
                run_id="r1",
                project=str(project),
            )
            candidate = _ingest_candidate(
                td,
                signal=_signal(scope="project"),
                project=str(project),
            )
            surface = GhostControlSurface.from_state_home(td)

            disable_status, disable_payload = surface.dispatch_action({"action": "disable_updates"})
            unconfirmed_status, unconfirmed_payload = surface.dispatch_action({
                "action": "delete_scope",
                "scope": "project",
                "project": str(project),
            })
            delete_status, delete_payload = surface.dispatch_action({
                "action": "delete_scope",
                "scope": "project",
                "project": str(project),
                "confirm": True,
            })
            remaining = GhostInboxStore(td).list_candidates(project=str(project))
            signal_rows = GhostSignalStore(td).read_all()

        self.assertEqual(disable_status, 200)
        self.assertTrue(disable_payload["ok"])
        self.assertFalse(disable_payload["enabled"])
        self.assertEqual(unconfirmed_status, 400)
        self.assertFalse(unconfirmed_payload["ok"])
        self.assertEqual(delete_status, 200)
        self.assertTrue(delete_payload["ok"])
        self.assertGreaterEqual(delete_payload["results"]["inbox"], 1)
        self.assertEqual(remaining, ())
        self.assertEqual(signal_rows, ())
        self.assertEqual(candidate.scope, "project")

    def test_reset_all_requires_confirm_and_preserves_update_setting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            candidate = _ingest_candidate(td)
            surface = GhostControlSurface.from_state_home(td)
            self.assertEqual(surface.dispatch_action({"action": "disable_updates"})[0], 200)
            self.assertEqual(surface.dispatch_action({"action": "accept_candidate", "id": candidate.id})[0], 200)

            unconfirmed_status, unconfirmed_payload = surface.dispatch_action({"action": "reset_all"})
            reset_status, reset_payload = surface.dispatch_action({
                "action": "reset_all",
                "confirm": True,
            })
            inbox = GhostInboxStore(td)
            candidates_after_reset = inbox.list_candidates()
            nodes_after_reset = GhostHebbianStore(td).list_nodes()
            learning_enabled = inbox.learning_enabled()

            self.assertEqual(unconfirmed_status, 400)
            self.assertFalse(unconfirmed_payload["ok"])
            self.assertEqual(reset_status, 200)
            self.assertTrue(reset_payload["ok"])
            self.assertEqual(candidates_after_reset, ())
            self.assertEqual(nodes_after_reset, ())
            self.assertFalse(learning_enabled)


if __name__ == "__main__":
    unittest.main()
