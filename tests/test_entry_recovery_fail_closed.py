"""Task entry recovery failures must fail closed, never degrade silently.

Locks:
- delivered construction failure is not an empty map (would re-execute).
- malformed recovered rows do not re-execute unsafe tools.
- recovery builder failure is an explicit ERROR, never a bare success.
- malformed turn values do not reset resume to 1 and collide identities.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "control"}))


def _row(turn: object, index: int, name: str = "edit"):
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    call = ToolCall(name=name, args={"path": "a.py", "content": "x\n"}, call_id=f"c{turn}-{index}")
    outcome = ToolOutcome("edited", True, audit={"changed": True}, changed=(name == "edit"))
    return SimpleNamespace(call=call, outcome=outcome, turn=turn, tool_index=index)


class EntryRecoveryFailClosedTests(unittest.TestCase):
    def test_delivered_construction_failure_is_not_empty_map(self) -> None:
        from codey.operations import task_entry as te
        from codey.operations.kernel_recovery import RecoveryFailed
        from codey.operations.task_session import TaskSession

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        frame = SimpleNamespace(
            run_id="r-entry-fail-1",
            recovered_tool_outcomes=(_row(1, 0),),
        )
        with mock.patch(
            "codey.operations.recovery.delivered_from_frame",
            side_effect=RuntimeError("boom"),
        ), self.assertRaises(RecoveryFailed):
            te._entry_recovery(frame, session)

    def test_malformed_row_does_not_reexecute_unsafe_tool(self) -> None:
        from codey.operations import task_entry as te
        from codey.operations.kernel_recovery import RecoveryFailed
        from codey.operations.task_session import TaskSession

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        bad = SimpleNamespace(call=None, outcome=None, turn="bad", tool_index="bad")
        frame = SimpleNamespace(run_id="r-entry-fail-2", recovered_tool_outcomes=(bad,))
        # Malformed turn identity is part of effect identity: must raise typed
        # RecoveryFailed, never silently resume at 1 and re-execute.
        with self.assertRaises(RecoveryFailed):
            te._entry_recovery(frame, session)

    def test_recovery_builder_failure_is_explicit_error_not_bare_success(self) -> None:
        from codey.operations import task_entry as te
        from codey.operations.task_session import TaskSession

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        frame = SimpleNamespace(
            run_id="r-entry-fail-3",
            recovered_tool_outcomes=(_row(1, 0),),
        )
        with mock.patch(
            "codey.operations.kernel_result.build_recovered_tool_result",
            side_effect=RuntimeError("builder boom"),
        ):
            # Builder outage is a recovery failure: either raise typed
            # RecoveryFailed or return explicit ERROR results (never bare
            # success, never empty success, executor never called).
            from codey.operations.kernel_errors import RecoveryFailed

            try:
                _delivered, _rows, _resume, initial = te._entry_recovery(frame, session)
            except RecoveryFailed as exc:
                self.assertTrue(
                    any(token in str(exc).lower() for token in ("recovered", "rebuild", "provenance", "recovery")),
                    f"RecoveryFailed must mention recovery, got {exc!r}",
                )
                return
            self.assertTrue(initial, "builder failure must not yield empty success list")
            for result in initial:
                text = str(getattr(result, "model_text", "") or "")
                self.assertTrue(
                    text.startswith("ERROR:"),
                    f"builder failure must be explicit ERROR, got {text!r}",
                )


if __name__ == "__main__":
    unittest.main()
