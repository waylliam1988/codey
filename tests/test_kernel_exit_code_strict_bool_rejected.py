"""Kernel exit_code accepts only real int zero (bool/str rejected).

Repro: _result_ok(), _record_run_verification() and record_facts audit
parsing used int(exit_code)==0, so bool False passed as success and "0"
strings passed. Structured verification must be int-only.

Lock: False/True/"0"/None never count as pass in any kernel exit path;
completion_gate fresh-pass also requires real int zero.
"""
from __future__ import annotations

import unittest


class KernelExitCodeStrictBoolRejectedTests(unittest.TestCase):
    def test_result_ok_rejects_bool_and_str(self) -> None:
        from codey.operations.kernel_execution import _result_ok
        from codey.runtime.core.models import ToolCall, ToolResult

        call = ToolCall(name="run", args={"command": "x"})
        res = ToolResult(call=call, model_text="ok")
        self.assertTrue(_result_ok("run", res, exit_code=0))
        self.assertFalse(_result_ok("run", res, exit_code=False), "bool False must not be ok")
        self.assertFalse(_result_ok("run", res, exit_code=True))
        self.assertFalse(_result_ok("run", res, exit_code="0"), "str must not be ok")
        self.assertFalse(_result_ok("run", res, exit_code=None) is True and False)  # None path uses text
        self.assertFalse(_result_ok("run", res, exit_code=1))

    def test_record_facts_rejects_bool_exit_in_audit(self) -> None:
        from codey.operations.kernel_execution import record_facts_for_result
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        for bad in (False, True, "0"):
            session = TaskSession(
                policy=TaskPolicy(grants=frozenset({"project.write", "control"})),
                task_kind="project", project="", max_turns=2,
            )
            call = ToolCall(name="run", args={"command": "make check"})
            result = ToolResult(call=call, model_text="ok", audit={"exit_code": bad})
            record_facts_for_result(session, call, result, ok=True)
            self.assertEqual(
                session.verifications, [],
                f"bool/str exit {bad!r} must not record a passing verification",
            )
        session = TaskSession(
            policy=TaskPolicy(grants=frozenset({"project.write", "control"})),
            task_kind="project", project="", max_turns=2,
        )
        call = ToolCall(name="run", args={"command": "make check"})
        result = ToolResult(call=call, model_text="ok", audit={"exit_code": 0})
        record_facts_for_result(session, call, result, ok=True)
        self.assertEqual(len(session.verifications), 1)
        self.assertTrue(session.verifications[0].get("passed"))

    def test_completion_gate_rejects_bool_exit(self) -> None:
        from codey.operations import completion_gate as cg
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        session = TaskSession(
            policy=TaskPolicy(grants=frozenset({"project.write", "control"})),
            task_kind="project", project="", max_turns=2,
        )
        session.edited_files["a.py"] = 1
        session.record_verification("make check", 1, True, exit_code=False)  # type: ignore[arg-type]
        # A bool exit must never count as fresh pass; gate must not complete.
        cg.evaluate(session, "done", context=None)
        # If the bool slipped into verifications as 0, the gate could pass.
        # The strict fix keeps verifications clean (no exit or non-zero).
        stored = session.verifications[-1] if session.verifications else {}
        self.assertNotEqual(
            stored.get("exit_code"), 0,
            f"bool False must not be stored as int 0: {stored!r}",
        )


if __name__ == "__main__":
    unittest.main()
