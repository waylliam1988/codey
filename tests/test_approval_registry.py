from __future__ import annotations

import threading
import unittest

from codey.agents.shell_approval import shell_command_payload
from codey.app.approval_registry import ApprovalRegistry
from codey.app.run_registry import RunSnapshot


def _complete(base: dict[str, object]) -> dict[str, object]:
    fields = shell_command_payload(str(base.get("command") or "test"))
    pending = dict(base)
    if "command" not in pending:
        pending["command"] = "test"
    pending["command_preview"] = fields["command"]
    pending["command_sha256"] = fields["command_sha256"]
    pending["command_chars"] = fields["command_chars"]
    pending["command_truncated"] = fields["command_truncated"]
    return pending


class ApprovalRegistryTests(unittest.TestCase):
    def test_cancel_teach_updates_the_waiting_callers_record_before_waking_it(self) -> None:
        approvals = ApprovalRegistry()
        wake = threading.Event()
        pending: dict[str, object] = {"event": wake, "cancelled": False}
        # sibling_probe.handle_control_teach waits on this event, then reads
        # cancellation from the same record it registered.
        approvals.add_teach("teach-1", pending)

        cancelled = approvals.cancel_teach()

        self.assertTrue(wake.is_set())
        self.assertIs(pending["cancelled"], True)
        self.assertEqual(len(cancelled), 1)
        self.assertIs(cancelled[0]["cancelled"], True)
        self.assertFalse(approvals.teach_snapshot())
        self.assertFalse(approvals.resume_teach("teach-1"))

    def test_resume_teach_wakes_without_marking_cancellation(self) -> None:
        approvals = ApprovalRegistry()
        wake = threading.Event()
        pending: dict[str, object] = {"event": wake, "cancelled": False}
        approvals.add_teach("teach-1", pending)
        snapshot = approvals.teach_snapshot()["teach-1"]
        snapshot["cancelled"] = True

        self.assertTrue(approvals.resume_teach("teach-1"))
        self.assertTrue(wake.is_set())
        self.assertIs(pending["cancelled"], False)
        self.assertFalse(approvals.resume_teach("missing"))

    def test_expire_shell_results_clears_pending_and_reports_denials(self) -> None:
        approvals = ApprovalRegistry()
        approvals.add_shell("shell-1", _complete({
            "id": "shell-1",
            "session_id": "session-1",
            "run_id": "run-1",
            "command": "pytest",
            "cwd": ".",
        }))

        events = approvals.expire_shell_results()

        self.assertEqual(approvals.shell_snapshot(), dict[str, dict[str, object]]())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "shell_result")
        self.assertFalse(events[0]["approved"])
        self.assertEqual(events[0]["command"], "pytest")
        self.assertEqual(events[0]["output"], "Task stopped; command approval expired.")

    def test_expire_shell_results_can_scope_by_run(self) -> None:
        approvals = ApprovalRegistry()
        approvals.add_shell("shell-old", _complete({
            "id": "shell-old",
            "session_id": "session-old",
            "run_id": "run-old",
            "command": "pytest",
            "cwd": ".",
        }))
        approvals.add_shell("shell-new", _complete({
            "id": "shell-new",
            "session_id": "session-new",
            "run_id": "run-new",
            "command": "ruff",
            "cwd": ".",
        }))

        events = approvals.expire_shell_results(
            run_id="run-old",
            output="expired",
        )

        event_ids: list[object] = [event["id"] for event in events]
        self.assertEqual(event_ids, list[str](["shell-old"]))
        self.assertEqual(events[0]["output"], "expired")
        remaining_ids: set[str] = set(approvals.shell_snapshot())
        self.assertEqual(remaining_ids, set[str](["shell-new"]))

    def test_expire_shell_results_can_keep_current_run(self) -> None:
        approvals = ApprovalRegistry()
        approvals.add_shell("shell-old", _complete({
            "id": "shell-old",
            "session_id": "session-old",
            "run_id": "run-old",
            "command": "pytest",
            "cwd": ".",
        }))
        approvals.add_shell("shell-current", _complete({
            "id": "shell-current",
            "session_id": "session-current",
            "run_id": "run-current",
            "command": "ruff",
            "cwd": ".",
        }))

        events = approvals.expire_shell_results(exclude_run_id="run-current")

        event_ids: list[object] = [event["id"] for event in events]
        self.assertEqual(event_ids, list[str](["shell-old"]))
        remaining_ids: set[str] = set(approvals.shell_snapshot())
        self.assertEqual(remaining_ids, set[str](["shell-current"]))

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
            dict[str, object](type="teach_request", run_id="run-new"),
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
            dict[str, object](type="teach_request", run_id="run-new"),
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
        approvals.add_shell("shell-a", _complete({"id": "shell-a", "session_id": "s-a"}))
        approvals.add_shell("shell-b", _complete({"id": "shell-b", "session_id": "s-b"}))

        events = approvals.expire_session("s-a")

        event_ids: list[object] = [event["id"] for event in events]
        self.assertEqual(event_ids, list[str](["shell-a"]))
        remaining_ids: set[str] = set(approvals.shell_snapshot())
        self.assertEqual(remaining_ids, set[str](["shell-b"]))


if __name__ == "__main__":
    unittest.main()
