"""Structured exit codes decide verification; text never implies pass.

Locks:
- ``run`` without a structured ``exit_code`` never records a passing
  verification (``not_run``), even when output contains ``pass``/``ok``.
- Only ``exit_code == 0`` with a matching workspace identity can complete.
- Injected executors returning ``pass`` text without an exit code stay blocked.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolCall, ToolResult


def _policy():
    return SimpleNamespace(
        allows=lambda grant: True,
        grants=frozenset(),
        strict_research=False,
        required_checks=(),
        to_payload=lambda: {},
    )


def _session():
    return TaskSession(policy=_policy(), task_kind="project", project="", max_turns=8)


class StructuredRunVerificationTests(unittest.TestCase):
    def test_text_pass_without_exit_code_is_not_run(self) -> None:
        from codey.operations import kernel_execution as ke

        session = _session()
        session.record_edit("a.py", revision=1)
        call = ToolCall(name="run", args={"command": "python -m unittest discover"}, call_id="c1")
        result = ToolResult(call=call, model_text="1 passed, all pass OK")
        ke.record_facts_for_result(session, call, result, ok=True, exit_code=None)
        # Must not record a passing verification without a structured exit code.
        passing = [v for v in session.verifications if bool(v.get("passed"))]
        self.assertEqual(passing, [], f"verifications={session.verifications}")

        from codey.operations.completion_gate import evaluate

        verdict = evaluate(session, "done summary", context=None)
        self.assertFalse(verdict.complete, f"must stay blocked, got {verdict}")

    def test_injected_run_text_pass_does_not_set_ok(self) -> None:
        from codey.operations import kernel_execution as ke

        session = _session()
        call = ToolCall(name="run", args={"command": "echo hi"}, call_id="c2")
        # Injected executor path: raw string result with pass text, no exit code.
        result, ok, opened, evidence, exit_code, handled = ke._run_via_delegate_or_fn(
            None, {"run": lambda c: "all pass OK"}, session, call, "run",
            active_turn=1, tool_index=0,
        )
        # Without a structured exit code the run must not be treated as ok-pass
        # for verification purposes; at minimum exit_code must stay None.
        self.assertIsNone(exit_code)
        # And a subsequent fact recording must not create a passing verification.
        session2 = _session()
        session2.record_edit("a.py", revision=1)
        ke.record_facts_for_result(session2, call, result, ok=ok, exit_code=exit_code)
        passing = [v for v in session2.verifications if bool(v.get("passed"))]
        self.assertEqual(passing, [])

    def test_zero_exit_code_with_matching_identity_passes(self) -> None:
        from codey.operations import kernel_execution as ke

        session = _session()
        session.record_edit("a.py", revision=1)
        session.set_workspace_state(1, session.workspace_fingerprint or "")
        call = ToolCall(name="run", args={"command": "python -m unittest discover"}, call_id="c3")
        result = ToolResult(call=call, model_text="OK")
        ke.record_facts_for_result(session, call, result, ok=True, exit_code=0)
        passing = [v for v in session.verifications if bool(v.get("passed"))]
        self.assertTrue(passing, "exit_code 0 must record a passing verification")

    def test_nonzero_exit_code_never_passes(self) -> None:
        from codey.operations import kernel_execution as ke

        session = _session()
        session.record_edit("a.py", revision=1)
        call = ToolCall(name="run", args={"command": "python -m unittest discover"}, call_id="c4")
        result = ToolResult(call=call, model_text="1 passed but exit 1")
        ke.record_facts_for_result(session, call, result, ok=False, exit_code=1)
        # ok=False path records nothing for run? Either way no passing verification.
        passing = [v for v in session.verifications if bool(v.get("passed"))]
        self.assertEqual(passing, [])


if __name__ == "__main__":
    unittest.main()
