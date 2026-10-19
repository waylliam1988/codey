from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.continuity import GhostContinuityResult, GhostContinuityStore
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.schema import GhostSignal
from codey.ghost.sleep import GhostSleepBudget, GhostSleepStore
from codey.ghost.work_queue import GhostWorkQueueStore
from tests.ghost_fixtures import event_text as sql_event_text


def _accepted_signal() -> GhostSignal:
    return GhostSignal(
        kind="style_preference",
        scope="user",
        summary="Prefer concise replies.",
        evidence_quote="以后回答短一点",
        confidence=0.94,
        metadata={"conflict_key": "reply_length", "value_key": "concise"},
        source="test",
    )


class GhostSleepTests(unittest.TestCase):
    def test_run_once_writes_bounded_report_without_transcript_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sleep = GhostSleepStore(td)

            report = sleep.run_once(
                trigger="post_turn",
                run_id="run-1",
                session_id="session-1",
                project=str(Path(td)),
            )
            payload = sleep.export_state()["sleep"]
            event_text = sql_event_text(sleep)

        self.assertFalse(report.cancelled)
        self.assertEqual(
            [step.name for step in report.steps],
            [
                "projection_health",
                "affinity_sync",
                "hebbian_decay",
                "affinity_decay",
                "continuity_refresh",
                "event_compaction",
                "report",
            ],
        )
        self.assertEqual(payload["report"]["cycle_id"], report.cycle_id)
        self.assertIn("ghost_sleep_report", event_text)
        forbidden = (
            "assistant reply body",
            "RAW SOURCE",
            "prompt text",
            "Local Context",
            "user original text",
        )
        for token in forbidden:
            self.assertNotIn(token, json.dumps(payload, ensure_ascii=False))

    def test_cancel_between_steps_persists_pending_steps(self) -> None:
        calls = 0

        def should_cancel() -> bool:
            nonlocal calls
            calls += 1
            return calls > 1

        with tempfile.TemporaryDirectory() as td:
            sleep = GhostSleepStore(td)

            report = sleep.run_once(should_cancel=should_cancel)
            payload = sleep.export_state()["sleep"]

        self.assertTrue(report.cancelled)
        self.assertEqual([step.name for step in report.steps], ["projection_health", "report"])
        self.assertIn("hebbian_decay", report.pending_steps)
        self.assertNotIn("report", report.pending_steps)
        self.assertEqual(payload["report"]["pending_steps"], list(report.pending_steps))

    def test_hebbian_decay_min_interval_noop_does_not_write_decay_event(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            created = inbox.ingest_signals(
                (_accepted_signal(),),
                session_id="s1",
                run_id="r1",
                user_text="以后回答短一点",
            )
            hebbian = GhostHebbianStore(td)
            hebbian.reinforce_candidate(created[0])
            before = sql_event_text(hebbian)
            sleep = GhostSleepStore(td)

            report = sleep.run_once(
                hebbian_store=hebbian,
                budget=GhostSleepBudget(hebbian_decay_min_interval_seconds=24 * 60 * 60),
            )
            after = sql_event_text(hebbian)

        decay_step = next(step for step in report.steps if step.name == "hebbian_decay")
        self.assertEqual(decay_step.skipped_reason, "min_interval")
        self.assertEqual(after, before)
        self.assertNotIn("ghost_hebbian_state_decayed", after)

    def test_sleep_syncs_affinity_from_hebbian_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            created = inbox.ingest_signals(
                (_accepted_signal(),),
                session_id="s1",
                run_id="r1",
                user_text="以后回答短一点",
            )
            reviewed = inbox.review_candidate(created[0].id, "accept", reviewed_by="test")
            assert reviewed is not None
            hebbian = GhostHebbianStore(td)
            hebbian.reinforce_candidate(reviewed)
            affinity = GhostAffinityStore(td)
            sleep = GhostSleepStore(td)

            report = sleep.run_once(
                hebbian_store=hebbian,
                affinity_store=affinity,
            )
            sync = next(step for step in report.steps if step.name == "affinity_sync")
            nodes = affinity.list_nodes(kind="user_preference")

        self.assertTrue(sync.ok)
        self.assertEqual(sync.counts["nodes_changed"], 1)
        self.assertEqual(len(nodes), 1)

    def test_sleep_reconciles_stale_work_queue_claim_without_executing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            continuity = GhostContinuityStore(td)
            continuity.sync_from_sources(
                knowledge_store=mock.Mock(
                    index=mock.Mock(
                        recent=mock.Mock(
                            return_value=[
                                {
                                    "id": "note-1",
                                    "type": "synthesis",
                                    "title": "Provider recovery",
                                    "body": "Research synthesis.",
                                    "open_questions": '["Should we keep tracking provider recovery?"]',
                                    "updated": "2999-01-01T00:00:00Z",
                                    "session_id": "s1",
                                    "project": "",
                                }
                            ]
                        )
                    )
                ),
                session_id="s1",
                run_id="r1",
                mode="chat",
            )
            queue = GhostWorkQueueStore(td)
            queue.sync_from_sources(continuity_store=continuity, session_id="s1")
            claimed = queue.claim_next(
                session_id="s1",
                run_id="run-1",
                user_request="continue",
                lease_seconds=-1,
            )
            sleep = GhostSleepStore(td)

            report = sleep.run_once(work_queue_store=queue)
            item = queue.list_items()[0]

        self.assertTrue(claimed.ok)
        compaction = next(step for step in report.steps if step.name == "event_compaction")
        self.assertEqual(compaction.counts["stale_work_claims"], 1)
        self.assertEqual(item.status, "queued")

    def test_continuity_refresh_passes_no_user_focus_excerpt(self) -> None:
        class FakeContinuity:
            def __init__(self) -> None:
                self.kwargs = None

            def sync_from_sources(self, **kwargs):
                self.kwargs = kwargs
                return GhostContinuityResult(True, items_changed=0, total_items=0)

            def compact_if_needed(self):
                return {"ok": True, "compacted": False}

        fake = FakeContinuity()
        with tempfile.TemporaryDirectory() as td:
            sleep = GhostSleepStore(td)

            report = sleep.run_once(
                continuity_store=fake,  # type: ignore[arg-type]
                run_id="run-1",
                session_id="session-1",
                project="project-1",
            )

        refresh = next(step for step in report.steps if step.name == "continuity_refresh")
        self.assertTrue(refresh.ok)
        self.assertIsNotNone(fake.kwargs)
        self.assertEqual(fake.kwargs["user_focus_excerpt"], "")
        self.assertEqual(fake.kwargs["mode"], "sleep")


if __name__ == "__main__":
    unittest.main()
