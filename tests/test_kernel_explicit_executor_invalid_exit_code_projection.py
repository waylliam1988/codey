"""Explicit executor invalid exit_code fails closed end to end.

Repro: explicit ``run`` executor returns ``audit={"exit_code": False}``.
The kernel must treat bool/str/float exits as invalid, mark ok=False,
record no verification, omit exit_code from event/UI/headless payloads
instead of projecting ``exit_code=0``.

Lock: execute_turn -> RunEvent -> run_event_ui_payload -> headless
_payload_tool keeps ok=False and carries no exit_code for every invalid
exit type; valid int 0 still passes.
"""
from __future__ import annotations

import unittest


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.read", "project.verify", "project.write", "control"}))


class ExplicitExecutorInvalidExitCodeProjectionTests(unittest.TestCase):
    def _run_once(self, bad):
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(
            policy=_policy(), task_kind="project", project="", max_turns=2
        )

        def fake(call: ToolCall):
            return ToolResult(call=call, model_text="ok", audit={"exit_code": bad})

        results = ke.execute_turn(
            session,
            [ToolCall(name="run", args={"command": "echo hi"})],
            executors={"run": fake},
            run_id="r-exit-proj-1", turn=1,
        )
        return session, results[0]

    def test_invalid_exits_fail_closed_through_all_projections(self) -> None:
        from codey.app import headless_runner as hr
        from codey.operations import kernel_events as kev
        from codey.runtime.observe.events import run_event_ui_payload

        for bad in (False, True, "0", 1.0, "1"):
            session, result = self._run_once(bad)
            identity = next(iter(session.executed))
            self.assertFalse(
                session.executed[identity].get("ok", True),
                f"executed.ok must be False for exit {bad!r}: {session.executed[identity]!r}",
            )
            self.assertEqual(
                session.verifications, [],
                f"invalid exit {bad!r} must record no verification",
            )
            events: list = []
            kev._emit_tool_results(
                events.append, session, [result], run_id="r-exit-proj-1", turn=1
            )
            event = events[0]
            self.assertFalse(event.outcome.ok, f"event ok must be False for {bad!r}")
            self.assertIsNone(
                event.outcome.exit_code,
                f"event exit_code must be omitted for {bad!r}, got {event.outcome.exit_code!r}",
            )
            payload = run_event_ui_payload("r", "s", event)
            assert payload is not None
            self.assertNotIn(
                "exit_code", payload,
                f"UI payload must omit invalid exit {bad!r}: {payload!r}",
            )
            headless = hr._payload_tool({"run_id": "r", "session_id": "s"}, payload)
            self.assertNotIn(
                "exit_code", headless,
                f"headless payload must omit invalid exit {bad!r}: {headless!r}",
            )
            self.assertFalse(headless.get("ok", True))

    def test_valid_int_zero_still_passes(self) -> None:
        from codey.app import headless_runner as hr
        from codey.operations import kernel_events as kev
        from codey.runtime.observe.events import run_event_ui_payload

        session, result = self._run_once(0)
        identity = next(iter(session.executed))
        self.assertTrue(session.executed[identity].get("ok", False))
        self.assertEqual(len(session.verifications), 1)
        events: list = []
        kev._emit_tool_results(
            events.append, session, [result], run_id="r-exit-proj-1", turn=1
        )
        event = events[0]
        self.assertTrue(event.outcome.ok)
        self.assertEqual(event.outcome.exit_code, 0)
        payload = run_event_ui_payload("r", "s", event)
        assert payload is not None
        self.assertEqual(payload.get("exit_code"), 0)
        headless = hr._payload_tool({"run_id": "r", "session_id": "s"}, payload)
        self.assertEqual(headless.get("exit_code"), 0)


if __name__ == "__main__":
    unittest.main()
