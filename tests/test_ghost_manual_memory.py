"""A saved experience can become a reviewed preference without another model call."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.directive import build_ghost_directive
from codey.ghost.hebbian import GhostReinforceResult
from codey.ghost.observations import GhostObservationStore


class GhostManualMemoryTests(unittest.TestCase):
    @staticmethod
    def _experience(home: str, *, session_id: str = "s1", run_id: str = "r1", project: str = "") -> None:
        assert GhostObservationStore(home).append_completed(
            run_id=run_id,
            session_id=session_id,
            project=project,
            mode="chat",
            user_text="Please keep answers concise.",
            assistant_text="Understood.",
            stop_reason="done",
        )

    @staticmethod
    def _proposal(**overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "action": "propose_preference",
            "id": "r1",
            "session_id": "s1",
            "project": "",
            "scope": "user",
            "conflict_key": "reply_length",
            "value_key": "concise",
            "evidence_quote": "keep answers concise",
        }
        body.update(overrides)
        return body

    def test_committed_experience_can_be_reviewed_and_used_by_directive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._experience(td)
            surface = GhostControlSurface.from_state_home(td)
            status, proposal = surface.dispatch_action(self._proposal())
            self.assertEqual(status, 200, proposal)
            self.assertTrue(proposal["ok"])
            self.assertEqual(proposal["candidate"]["status"], "candidate")
            self.assertEqual(surface.summary(session_id="s1")["counts"]["review"], 1)
            self.assertEqual(surface.summary(session_id="s1")["counts"]["active"], 0)

            candidate_id = proposal["candidate"]["id"]
            accepted_status, accepted = surface.dispatch_action({
                "action": "accept_candidate", "id": candidate_id, "session_id": "s1",
            })
            self.assertEqual(accepted_status, 200, accepted)
            self.assertTrue(accepted["state_update"]["applied"])
            self.assertIn("reply length = concise", build_ghost_directive(surface.hebbian, session_id="s1").text)

    def test_proposal_requires_matching_committed_source_and_grounded_quote(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._experience(td)
            surface = GhostControlSurface.from_state_home(td)
            for changes in (
                {"session_id": "other"},
                {"session_id": ""},
                {"id": "missing"},
                {"evidence_quote": "Please use tables"},
                {"conflict_key": "state_backend", "value_key": "jsonl"},
                {"scope": "project"},
            ):
                with self.subTest(changes=changes):
                    status, payload = surface.dispatch_action(self._proposal(**changes))
                    self.assertNotEqual(status, 200, payload)
                    self.assertFalse(payload["ok"])
            self.assertEqual(surface.summary(session_id="s1")["counts"]["review"], 0)

    def test_disabled_updates_block_manual_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._experience(td)
            surface = GhostControlSurface.from_state_home(td)
            self.assertEqual(surface.dispatch_action({"action": "disable_updates"})[0], 200)
            status, payload = surface.dispatch_action(self._proposal())
            self.assertNotEqual(status, 200, payload)
            self.assertEqual(surface.summary(session_id="s1")["counts"]["review"], 0)

    def test_reproposing_reviewed_preference_does_not_claim_pending_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._experience(td)
            surface = GhostControlSurface.from_state_home(td)
            _, first = surface.dispatch_action(self._proposal())
            candidate_id = first["candidate"]["id"]
            self.assertEqual(surface.dispatch_action({
                "action": "accept_candidate", "id": candidate_id, "session_id": "s1",
            })[0], 200)

            status, payload = surface.dispatch_action(self._proposal())

            self.assertEqual(status, 409, payload)
            self.assertFalse(payload["ok"])
            self.assertEqual(surface.summary(session_id="s1")["counts"]["review"], 0)

    def test_rejected_preference_can_be_proposed_again_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._experience(td)
            surface = GhostControlSurface.from_state_home(td)
            _, first = surface.dispatch_action(self._proposal())
            first_id = first["candidate"]["id"]
            self.assertEqual(surface.dispatch_action({
                "action": "reject_candidate", "id": first_id, "session_id": "s1",
            })[0], 200)

            status, second = surface.dispatch_action(self._proposal())

            self.assertEqual(status, 200, second)
            self.assertEqual(second["candidate"]["status"], "candidate")
            self.assertNotEqual(second["candidate"]["id"], first_id)
            self.assertEqual(surface.summary(session_id="s1")["counts"]["review"], 1)

    def test_failed_reinforcement_is_visible_and_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._experience(td)
            surface = GhostControlSurface.from_state_home(td)
            _, proposed = surface.dispatch_action(self._proposal())
            candidate_id = proposed["candidate"]["id"]
            action = {"action": "accept_candidate", "id": candidate_id, "session_id": "s1"}
            with mock.patch.object(
                surface.hebbian, "reinforce_candidate",
                return_value=GhostReinforceResult(False, "event_write_failed"),
            ):
                status, failed = surface.dispatch_action(action)

            self.assertEqual(status, 500, failed)
            self.assertFalse(failed["ok"])
            summary = surface.summary(session_id="s1")
            self.assertEqual(summary["counts"]["active"], 0)
            self.assertEqual(summary["counts"]["repair"], 1)
            self.assertEqual(summary["repair"][0]["id"], candidate_id)

            retry_status, retried = surface.dispatch_action(action)
            self.assertEqual(retry_status, 200, retried)
            self.assertTrue(retried["state_update"]["applied"])
            self.assertIn("reply length = concise", build_ghost_directive(surface.hebbian, session_id="s1").text)

    def test_node_read_failure_is_reported_without_false_repair_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._experience(td)
            surface = GhostControlSurface.from_state_home(td)
            _, proposed = surface.dispatch_action(self._proposal())
            self.assertEqual(surface.dispatch_action({
                "action": "accept_candidate", "id": proposed["candidate"]["id"], "session_id": "s1",
            })[0], 200)
            with mock.patch.object(surface.hebbian, "list_nodes", side_effect=OSError("read failed")):
                summary = surface.summary(session_id="s1")

            self.assertGreater(summary["counts"]["warnings"], 0)
            self.assertEqual(summary["counts"]["repair"], 0)

    def test_project_source_must_match_active_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            other = Path(td, "other")
            project.mkdir()
            other.mkdir()
            self._experience(td, project=str(project))
            surface = GhostControlSurface.from_state_home(td)
            bad_status, _ = surface.dispatch_action(self._proposal(project=str(other), scope="project"))
            good_status, good = surface.dispatch_action(self._proposal(project=str(project), scope="project"))
            self.assertNotEqual(bad_status, 200)
            self.assertEqual(good_status, 200, good)


if __name__ == "__main__":
    unittest.main()
