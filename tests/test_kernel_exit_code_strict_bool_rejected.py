"""Kernel exit_code accepts only real int zero (bool/str rejected).

Repro: result_ok(), _record_run_verification() and record_facts audit
parsing used int(exit_code)==0, so bool False passed as success and "0"
strings passed. Structured verification must be int-only.

Lock: False/True/"0"/None never count as pass in any kernel exit path;
completion_gate fresh-pass also requires real int zero.
"""
from __future__ import annotations

import unittest


class KernelExitCodeStrictBoolRejectedTests(unittest.TestCase):
    def testresult_ok_rejects_bool_and_str(self) -> None:
        from codey.operations.kernel_result import result_ok
        from codey.runtime.core.models import ToolCall, ToolResult

        call = ToolCall(name="run", args={"command": "x"})
        res = ToolResult(ok=True, call=call, model_text="ok")
        self.assertTrue(result_ok("run", res, exit_code=0))
        self.assertFalse(result_ok("run", res, exit_code=False), "bool False must not be ok")
        self.assertFalse(result_ok("run", res, exit_code=True))
        self.assertFalse(result_ok("run", res, exit_code="0"), "str must not be ok")
        # Run without a structured exit never reports success, even for plain text.
        self.assertFalse(result_ok("run", res, exit_code=None))
        err = ToolResult(ok=False, call=call, model_text="ERROR: boom")
        self.assertFalse(result_ok("run", err, exit_code=None))
        self.assertFalse(result_ok("run", res, exit_code=1))
        # Audit-carried exits are strict as well: bool/str audit never passes.
        for bad in (False, True, "0", 1.0):
            bad_res = ToolResult(ok=True, call=call, model_text="ok", audit={"exit_code": bad})
            self.assertFalse(result_ok("run", bad_res), f"audit exit {bad!r} must not be ok")
        # Missing audit exit is also failure for run.
        self.assertFalse(result_ok("run", res))

    def test_record_facts_rejects_bool_exit_in_audit(self) -> None:
        from codey.operations.kernel_facts import record_facts_for_result
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        for bad in (False, True, "0"):
            session = TaskSession(
                policy=TaskPolicy(grants=frozenset({"project.write", "control"})),
                task_kind="project", project="", max_turns=2,
            )
            call = ToolCall(name="run", args={"command": "make check"})
            result = ToolResult(ok=True, call=call, model_text="ok", audit={"exit_code": bad})
            record_facts_for_result(session, call, result, ok=False)
            self.assertEqual(len(session.verifications), 1)
            self.assertFalse(session.verifications[0]["passed"])
            self.assertNotIn("exit_code", session.verifications[0])
        session = TaskSession(
            policy=TaskPolicy(grants=frozenset({"project.write", "control"})),
            task_kind="project", project="", max_turns=2,
        )
        call = ToolCall(name="run", args={"command": "make check"})
        result = ToolResult(ok=True, call=call, model_text="ok", audit={"exit_code": 0})
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
