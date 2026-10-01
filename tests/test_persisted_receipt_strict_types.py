"""Persisted effect receipt fields reject type confusion during replay."""
from __future__ import annotations

import unittest


class PersistedReceiptStrictTypesTests(unittest.TestCase):
    def test_string_false_ok_does_not_replay_as_success(self) -> None:
        from codey.operations.kernel_recovery import replay_slot_typed
        from codey.runtime.core.models import ToolCall

        class Session:
            executed = {
                "effect": {
                    "name": "read_file",
                    "ok": "false",
                    "call_id": "c1",
                    "args_digest": "bad",
                    "excerpt": "old",
                }
            }
            _memory_results = {}

        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        slot = replay_slot_typed(Session(), "effect", call, "read_file", 1)
        self.assertEqual(slot.disposition, "FAILED")

    def test_numeric_zero_ok_does_not_replay_as_success(self) -> None:
        from codey.operations.kernel_recovery import replay_slot_typed
        from codey.runtime.core.models import ToolCall

        class Session:
            executed = {
                "effect": {
                    "name": "read_file",
                    "ok": 0,
                    "call_id": "c1",
                    "args_digest": "bad",
                    "excerpt": "old",
                }
            }
            _memory_results = {}

        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        slot = replay_slot_typed(Session(), "effect", call, "read_file", 1)
        self.assertEqual(slot.disposition, "FAILED")

    def test_present_non_string_receipt_fields_fail_closed(self) -> None:
        from codey.operations.kernel_recovery import replay_slot_typed
        from codey.runtime.core.models import ToolCall

        for field in ("name", "call_id", "args_digest", "excerpt"):
            record = {
                "name": "read_file",
                "ok": False,
                "call_id": "c1",
                "args_digest": "bad",
                "excerpt": "old",
            }
            record[field] = 0

            class Session:
                executed = {"effect": record}
                _memory_results = {}

            call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
            slot = replay_slot_typed(Session(), "effect", call, "read_file", 1)
            self.assertEqual(slot.disposition, "FAILED", field)

    def test_recovery_exception_does_not_treat_string_ok_as_successful_intent(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from codey.operations import kernel_execution
        from codey.runtime.core.models import ToolCall, ToolResult

        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        session = SimpleNamespace(executed={"effect": {"ok": "false"}})
        reconcile = mock.Mock()
        settle = mock.Mock()
        guarded = ToolResult(call=call, model_text="ERROR: guarded")

        with mock.patch(
            "codey.operations.kernel_recovery.replay_slot_typed",
            side_effect=RuntimeError("replay unavailable"),
        ):
            kernel_execution._reconcile_guarded_slot(
                session,
                "effect",
                call,
                "read_file",
                1,
                None,
                None,
                (),
                None,
                reconcile,
                settle,
                guarded,
            )

        settle.assert_not_called()
        reconcile.assert_called_once_with("effect", ok=False, result=guarded)

    def test_recovery_exception_missing_ok_does_not_treat_receipt_as_successful_intent(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from codey.operations import kernel_execution
        from codey.runtime.core.models import ToolCall, ToolResult

        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        session = SimpleNamespace(executed={"effect": {}})
        reconcile = mock.Mock()
        settle = mock.Mock()
        guarded = ToolResult(call=call, model_text="ERROR: guarded")

        with mock.patch(
            "codey.operations.kernel_recovery.replay_slot_typed",
            side_effect=RuntimeError("replay unavailable"),
        ):
            kernel_execution._reconcile_guarded_slot(
                session,
                "effect",
                call,
                "read_file",
                1,
                None,
                None,
                (),
                None,
                reconcile,
                settle,
                guarded,
            )

        settle.assert_not_called()
        reconcile.assert_called_once_with("effect", ok=False, result=guarded)

    def test_string_false_verification_never_reports_success(self) -> None:
        from types import SimpleNamespace

        from codey.operations.project_adapter import _session_checks_passed

        illegal = {"command": "pytest", "revision": 1, "passed": "false"}

        # Gate view blocks: illegal never reports success, and fail-closed
        # False stays blocked (never becomes success).
        self.assertFalse(_session_checks_passed(SimpleNamespace(verifications=[illegal])))
        self.assertFalse(
            _session_checks_passed(
                SimpleNamespace(verifications=[{"command": "pytest", "revision": 1, "passed": False}])
            )
        )
        self.assertTrue(
            _session_checks_passed(
                SimpleNamespace(verifications=[{"command": "pytest", "revision": 1, "passed": True}])
            )
        )

    def test_event_projection_does_not_coerce_string_receipt_ok_to_success(self) -> None:
        from codey.operations import kernel_events
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read", "control"})))
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        identity = turn_effect_id("run", 1, 0)
        session.executed[identity] = {"ok": "false", "name": "read_file"}
        result = ToolResult(call=call, model_text="content")
        events = []

        kernel_events._emit_tool_results(events.append, session, [result], run_id="run", turn=1)

        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].outcome.ok)

    def test_project_adapter_does_not_report_string_false_checks_as_passed(self) -> None:
        from types import SimpleNamespace

        from codey.operations.project_adapter import _session_checks_passed

        self.assertFalse(
            _session_checks_passed(SimpleNamespace(verifications=[{"passed": "false"}]))
        )
        self.assertTrue(
            _session_checks_passed(SimpleNamespace(verifications=[{"passed": True}]))
        )


if __name__ == "__main__":
    unittest.main()
