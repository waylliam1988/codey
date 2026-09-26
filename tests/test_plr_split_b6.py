"""PLR split B6 parity tests: cmd_ghost dispatch + headless payload/receipt.

Pure-extraction guard: dispatch conditions, JSON structure, exit codes unchanged.
"""
from __future__ import annotations

import io
import json
import unittest
from unittest import mock

import codey.app.cli as cli
from codey.app.headless_runner import _bounded_receipt, headless_event_payload


def _run_ghost(action, **kwargs):
    args = mock.Mock(ghost_cmd=action, state_home="/tmp/nope", **kwargs)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
        code = cli.cmd_ghost(args)
    return code, stdout.getvalue(), stderr.getvalue()


class GhostDispatchParityTests(unittest.TestCase):
    def _surface(self, **overrides):
        surface = mock.Mock()
        surface.available = True
        surface.inbox = mock.Mock()
        surface.hebbian = mock.Mock()
        surface.continuity = mock.Mock()
        surface.affinity = mock.Mock()
        surface.work_queue = mock.Mock()
        for k, v in overrides.items():
            setattr(surface, k, v)
        return surface

    def test_unavailable_returns_1(self) -> None:
        surface = self._surface()
        surface.available = False
        surface.inbox = None
        with mock.patch(
            "codey.ghost.control_surface.GhostControlSurface.from_state_home",
            return_value=surface,
        ):
            code, out, _ = _run_ghost("list")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_list_export_work_list_dispatch(self) -> None:
        surface = self._surface()
        surface.inbox.list_candidates.return_value = []
        surface.inbox.learning_enabled.return_value = True
        surface.inbox.last_warnings = []
        surface.export_state.return_value = {"ok": True}
        surface.work_queue.list_items.return_value = []
        surface.work_queue.last_warnings = []
        with mock.patch(
            "codey.ghost.control_surface.GhostControlSurface.from_state_home",
            return_value=surface,
        ):
            for action in ("list", "export", "work-list"):
                code, out, _ = _run_ghost(
                    action, status="", scope="", project="", session_id="", kind=""
                )
                self.assertEqual(code, 0, action)
                self.assertTrue(json.loads(out)["ok"], action)

    def test_work_queue_reject_not_found_returns_1(self) -> None:
        surface = self._surface()
        surface.work_queue.queue_item.return_value = None
        surface.work_queue.reject_item.return_value = None
        with mock.patch(
            "codey.ghost.control_surface.GhostControlSurface.from_state_home",
            return_value=surface,
        ):
            for action in ("work-queue", "work-reject"):
                code, out, _ = _run_ghost(action, item_id="missing")
                self.assertEqual(code, 1, action)
                self.assertFalse(json.loads(out)["ok"])

    def test_rebuild_reset_delete_require_yes(self) -> None:
        surface = self._surface()
        with mock.patch(
            "codey.ghost.control_surface.GhostControlSurface.from_state_home",
            return_value=surface,
        ):
            for action in (
                "rebuild-state",
                "rebuild-continuity",
                "rebuild-affinity",
                "reset",
                "delete-scope",
            ):
                kwargs = {"yes": False}
                if action == "delete-scope":
                    kwargs.update(
                        {"scope_name": "user", "project": "", "session_id": ""}
                    )
                code, out, _ = _run_ghost(action, **kwargs)
                self.assertEqual(code, 2, action)
                self.assertFalse(json.loads(out)["ok"])

    def test_unknown_action_returns_2(self) -> None:
        surface = self._surface()
        with mock.patch(
            "codey.ghost.control_surface.GhostControlSurface.from_state_home",
            return_value=surface,
        ):
            code, _, err = _run_ghost("nope")
            self.assertEqual(code, 2)
            self.assertIn("ghost subcommand required", err)


class HeadlessPayloadParityTests(unittest.TestCase):
    def test_all_event_types_bounded(self) -> None:
        base = {"run_id": "r1", "session_id": "s1"}
        cases = [
            ({"type": "task_start", "project": "p", "provider": "q", "mode": "m",
              "max_turns": 3}, "project"),
            ({"type": "status", "status": "running"}, "status"),
            ({"type": "info", "text": "hi"}, "text"),
            ({"type": "shell_request", "id": "x"}, "id"),
            ({"type": "turn", "turn": 1}, "turn"),
            ({"type": "tool_started", "turn": 1, "tool_id": "t",
              "kind": "k"}, "tool"),
            ({"type": "tool", "turn": 1, "tool_id": "t", "kind": "k"}, "ok"),
            ({"type": "task_done", "summary": "s", "stop_reason": "done",
              "turns": 1, "max_turns": 2}, "stop_reason"),
            ({"type": "headless_close", "stop_reason": "close_incomplete",
              "exit_code": 1}, "exit_code"),
        ]
        for event, key in cases:
            payload = headless_event_payload({**base, **event})
            self.assertIsNotNone(payload, event["type"])
            assert payload is not None
            self.assertEqual(payload["type"], event["type"])
            self.assertEqual(payload["schema_version"], 1)
            self.assertIn(key, payload)

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(headless_event_payload({"type": "nope"}))
        self.assertIsNone(headless_event_payload("bad"))  # type: ignore[arg-type]

    def test_task_done_nests_bounded_receipt(self) -> None:
        payload = headless_event_payload({
            "type": "task_done", "run_id": "r", "session_id": "s",
            "receipt": {"schema_version": 1,
                        "display": {"summary": "ok"},
                        "work": {"changed_count": 2, "mode": "project"},
                        "verification": {"trust": "high", "checks_passed": True},
                        "integrity": {"status": "ok", "severity": "none"}},
        })
        assert payload is not None
        receipt = payload["receipt"]
        assert isinstance(receipt, dict)
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["display"], {"summary": "ok"})


class BoundedReceiptParityTests(unittest.TestCase):
    def test_empty_receipt(self) -> None:
        self.assertEqual(_bounded_receipt({}), {})

    def test_all_sections(self) -> None:
        out = _bounded_receipt({
            "schema_version": 1,
            "display": {"summary": "s", "detail": "d"},
            "work": {"changed_count": "3", "mode": "m", "restore_available": 1},
            "verification": {"trust": "t", "checks_passed": 1, "state": "st",
                             "proof_refs": ["a", "", "b", "c"]},
            "integrity": {"status": "ok", "severity": "low",
                          "reason_codes": ["c1", ""],
                          "affected_paths": ["p1", "p2", "p3", "p4", "p5"],
                          "refs": ["r1", "", "r2"]},
        })
        self.assertEqual(out["schema_version"], 1)
        self.assertEqual(out["display"], {"summary": "s", "detail": "d"})
        self.assertEqual(out["work"]["changed_count"], 3)
        self.assertIn("proof_refs", out["verification"])
        assert isinstance(out["verification"], dict)
        self.assertLessEqual(len(out["verification"]["proof_refs"]), 2)
        assert isinstance(out["integrity"], dict)
        self.assertLessEqual(len(out["integrity"]["affected_paths"]), 4)


if __name__ == "__main__":
    unittest.main()
