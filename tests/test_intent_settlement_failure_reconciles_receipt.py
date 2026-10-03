"""Intent settlement failure must reconcile the existing receipt.

Locks P2 settlement atomicity:
- ``session.executed`` written + ``intent_sink.settle`` failed
- next recovery replays the receipt without re-executing the unsafe tool
"""
from __future__ import annotations

import unittest


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "project.read", "control"}))


class IntentSettlementReconcilesReceiptTests(unittest.TestCase):
    def test_executed_plus_pending_intent_never_reexecutes(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.runtime.core.models import ToolCall

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="edit", args={"path": "a.py", "content": "x\n"}, call_id="c1")
        identity = turn_effect_id("r-reconcile-1:task", 1, 0)
        from codey.runtime.effects.effect_records import compute_args_digest

        session.executed[identity] = {
            "name": "edit",
            "ok": True,
            "call_id": "c1",
            "excerpt": "edited",
            "args_digest": compute_args_digest(call.args),
            # Note: no verified provenance on purpose; the point here is
            # no second execution. Provenance failure is an explicit error,
            # never a silent re-execution.
            "workspace_revision": 0,
            "workspace_fingerprint": "",
        }

        class PendingSink:
            def has_unsettled(self, _identity: str) -> bool:
                return True

            def settle(self, _identity: str, _ok: bool, *, result=None, exit_code=None) -> None:
                raise AssertionError("must not settle during guard check")

            def begin_turn(self, _items, **_kwargs) -> None:
                pass

        calls = 0

        def evil_edit(_c):
            nonlocal calls
            calls += 1
            from codey.runtime.core.models import ToolResult

            return ToolResult(ok=True, call=_c, model_text="second execution")

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            results = execute_turn(
                session, [call],
                executors={"edit": evil_edit},
                run_id="r-reconcile-1", effect_scope="task", turn=1,
                project_path=project,
                intent_sink=PendingSink(),
            )
        self.assertEqual(calls, 0, "executed+pending intent must never re-execute unsafe")
        self.assertTrue(results)

    def test_durable_receipt_survives_intent_settle_outage(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")

        def fake_read(_c: ToolCall):
            return ToolResult(ok=True, call=_c, model_text="content")

        class FlakySink:
            def __init__(self):
                self.settle_calls = 0

            def has_unsettled(self, _identity: str) -> bool:
                return False

            def begin_turn(self, _items, **_kwargs) -> None:
                pass

            def settle(self, _identity: str, _ok: bool, *, result=None, exit_code=None) -> None:
                self.settle_calls += 1
                raise RuntimeError("intent store boom")

        import contextlib

        sink = FlakySink()
        with contextlib.suppress(Exception):
            execute_turn(
                session, [call],
                executors={"read_file": fake_read},
                run_id="r-reconcile-2", effect_scope="task", turn=1,
                intent_sink=sink,
            )
        # Durable receipt must exist even though intent settlement failed.
        self.assertEqual(len(session.executed), 1)
        # Second turn with a healthy sink replays without re-executing.
        calls = 0

        def evil_read(_c: ToolCall):
            nonlocal calls
            calls += 1
            return ToolResult(ok=True, call=_c, model_text="second")

        class HealthySink:
            def has_unsettled(self, _identity: str) -> bool:
                return False

            def begin_turn(self, _items, **_kwargs) -> None:
                pass

            def settle(self, _identity: str, _ok: bool, *, result=None, exit_code=None) -> None:
                pass

        results = execute_turn(
            session, [call],
            executors={"read_file": evil_read},
            run_id="r-reconcile-2", effect_scope="task", turn=1,
            intent_sink=HealthySink(),
        )
        self.assertEqual(calls, 0)
        self.assertEqual(str(results[0].model_text or ""), "content")


if __name__ == "__main__":
    unittest.main()
