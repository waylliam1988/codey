from __future__ import annotations

import unittest

from codey.app.approval_registry import ApprovalRegistry
from codey.app.run_registry import RunSnapshot


class ApprovalRegistryTests(unittest.TestCase):
    def test_expire_shell_results_clears_pending_and_reports_denials(self) -> None:
        approvals = ApprovalRegistry()
        approvals.add_shell("shell-1", {
            "id": "shell-1",
            "session_id": "session-1",
            "run_id": "run-1",
            "command": "pytest",
            "cwd": ".",
        })

        events = approvals.expire_shell_results()

        self.assertEqual(approvals.shell_snapshot(), {})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "shell_result")
        self.assertFalse(events[0]["approved"])
        self.assertEqual(events[0]["command"], "pytest")
        self.assertEqual(events[0]["output"], "Task stopped; command approval expired.")

    def test_expire_shell_results_can_scope_by_run(self) -> None:
        approvals = ApprovalRegistry()
        approvals.add_shell("shell-old", {
            "id": "shell-old",
            "session_id": "session-old",
            "run_id": "run-old",
            "command": "pytest",
            "cwd": ".",
        })
        approvals.add_shell("shell-new", {
            "id": "shell-new",
            "session_id": "session-new",
            "run_id": "run-new",
            "command": "ruff",
            "cwd": ".",
        })

        events = approvals.expire_shell_results(
            run_id="run-old",
            output="expired",
        )

        self.assertEqual([event["id"] for event in events], ["shell-old"])
        self.assertEqual(events[0]["output"], "expired")
        self.assertEqual(set(approvals.shell_snapshot()), {"shell-new"})

    def test_expire_shell_results_can_keep_current_run(self) -> None:
        approvals = ApprovalRegistry()
        approvals.add_shell("shell-old", {
            "id": "shell-old",
            "session_id": "session-old",
            "run_id": "run-old",
            "command": "pytest",
            "cwd": ".",
        })
        approvals.add_shell("shell-current", {
            "id": "shell-current",
            "session_id": "session-current",
            "run_id": "run-current",
            "command": "ruff",
            "cwd": ".",
        })

        events = approvals.expire_shell_results(exclude_run_id="run-current")

        self.assertEqual([event["id"] for event in events], ["shell-old"])
        self.assertEqual(set(approvals.shell_snapshot()), {"shell-current"})

    def test_pending_ui_event_prefers_active_run_scope(self) -> None:
        approvals = ApprovalRegistry()
        approvals.add_shell("old", {
            "ui_event": {"type": "shell_request", "run_id": "run-old"}
        })
        approvals.add_teach("new", {
            "ui_event": {"type": "teach_request", "run_id": "run-new"}
        })
        active = RunSnapshot(
            run_id="run-new",
            session_id="session-1",
            project=None,
            task="hello",
            provider_id="deepseek",
        )

        self.assertEqual(
            approvals.pending_ui_event(active),
            {"type": "teach_request", "run_id": "run-new"},
        )
        self.assertIsNone(approvals.pending_ui_event(
            RunSnapshot(
                run_id="run-other",
                session_id="session-1",
                project=None,
                task="hello",
                provider_id="deepseek",
            )
        ))

    def test_pending_ui_event_without_active_run_returns_latest(self) -> None:
        approvals = ApprovalRegistry()
        approvals.add_shell("old", {
            "ui_event": {"type": "shell_request", "run_id": "run-old"}
        })
        approvals.add_teach("new", {
            "ui_event": {"type": "teach_request", "run_id": "run-new"}
        })

        self.assertEqual(
            approvals.pending_ui_event(None),
            {"type": "teach_request", "run_id": "run-new"},
        )

    def test_stop_all_bumps_generation_even_when_map_is_empty(self) -> None:
        approvals = ApprovalRegistry()
        approvals.add_shell("shell-1", {"id": "shell-1", "session_id": "s-1"})
        claimed = approvals.pop_shell("shell-1")
        assert claimed is not None
        claimed_generation = claimed.pop("_approval_generation", 0)

        # Stop lands after the Allow claimed its entry: the map is empty but
        # the epoch must still advance so the claim detects it.
        approvals.expire_shell_results()

        self.assertNotEqual(claimed_generation, approvals.current_generation())

    def test_add_shell_pins_current_generation(self) -> None:
        approvals = ApprovalRegistry()
        approvals.expire_shell_results()
        bumped = approvals.current_generation()
        approvals.add_shell("shell-2", {"id": "shell-2"})

        pending = approvals.pop_shell("shell-2")
        assert pending is not None
        self.assertEqual(pending.get("_approval_generation"), bumped)

    def test_expire_session_only_clears_matching_session(self) -> None:
        approvals = ApprovalRegistry()
        approvals.add_shell("shell-a", {"id": "shell-a", "session_id": "s-a"})
        approvals.add_shell("shell-b", {"id": "shell-b", "session_id": "s-b"})

        events = approvals.expire_session("s-a")

        self.assertEqual([event["id"] for event in events], ["shell-a"])
        self.assertEqual(set(approvals.shell_snapshot()), {"shell-b"})


if __name__ == "__main__":
    unittest.main()
