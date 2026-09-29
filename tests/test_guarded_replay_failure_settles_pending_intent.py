"""Replay guard failures must settle fresh pending intents exactly once."""
from __future__ import annotations

import unittest
from unittest import mock


class GuardedReplayFailureSettlesPendingIntentTests(unittest.TestCase):
    def test_replay_check_exception_settles_guard_error(self) -> None:
        from codey.operations import kernel_execution
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.runtime.core.models import ToolCall, ToolResult

        session = object()
        call = ToolCall(name="shell", args={"command": "echo hi"}, call_id="c1")
        guarded = ToolResult(call=call, model_text="ERROR: recovery failed")
        settled = []

        with mock.patch(
            "codey.operations.kernel_recovery.replay_slot_typed",
            side_effect=RecoveryFailed("replay store unavailable"),
        ):
            kernel_execution._reconcile_guarded_slot(
                session,
                "effect-1",
                call,
                "shell",
                1,
                None,
                None,
                (),
                object(),
                lambda *_args, **_kwargs: None,
                lambda *_args, **_kwargs: settled.append(_args),
                guarded,
            )

        self.assertEqual(len(settled), 1)

    def test_replay_check_settlement_failure_is_not_suppressed(self) -> None:
        from codey.operations import kernel_execution
        from codey.operations.kernel_errors import EffectSettlementFailed, RecoveryFailed
        from codey.runtime.core.models import ToolCall, ToolResult

        session = object()
        call = ToolCall(name="shell", args={"command": "echo hi"}, call_id="c1")
        guarded = ToolResult(call=call, model_text="ERROR: recovery failed")

        def fail_settle(*_args, **_kwargs):
            raise EffectSettlementFailed("receipt unavailable")

        with mock.patch(
            "codey.operations.kernel_recovery.replay_slot_typed",
            side_effect=RecoveryFailed("replay store unavailable"),
        ), self.assertRaises(EffectSettlementFailed):
            kernel_execution._reconcile_guarded_slot(
                session,
                "effect-1",
                call,
                "shell",
                1,
                None,
                None,
                (),
                object(),
                lambda *_args, **_kwargs: None,
                fail_settle,
                guarded,
            )


if __name__ == "__main__":
    unittest.main()
