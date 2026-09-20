"""P1: provider timeout modes + kill-point matrix (provider/tool/delivery/shell).

Timeout never equals "the provider did nothing": ``execute_then_timeout``
means the durable side must stay UNKNOWN, never success.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.runtime.effects.effect_records import RuntimeEffectError
from tests.stress.model import normalize
from tests.stress.oracle import InvariantChecker
from tests.stress.world import FakeProvider, FakeProviderTimeout, StressWorld


def _world(seed: int = 11):
    tmp = tempfile.TemporaryDirectory()
    world = StressWorld(Path(tmp.name) / "state", seed=seed)
    world.keepalive = tmp
    return world


class ProviderTimeoutModeTests(unittest.TestCase):
    def test_execute_then_timeout_records_and_raises(self) -> None:
        provider = FakeProvider("execute_then_timeout")
        with self.assertRaises(FakeProviderTimeout):
            provider.send({"id": "req-1"})
        self.assertEqual([r["id"] for r in provider.received], ["req-1"])
        self.assertEqual([r["id"] for r in provider.executed], ["req-1"])

    def test_timeout_before_execute_records_nothing_and_raises(self) -> None:
        provider = FakeProvider("timeout_before_execute")
        with self.assertRaises(FakeProviderTimeout):
            provider.send({"id": "req-1"})
        self.assertEqual(provider.received, [])
        self.assertEqual(provider.executed, [])

    def test_normal_mode_replies(self) -> None:
        provider = FakeProvider("normal")
        reply = provider.send({"id": "req-1"})
        self.assertEqual(reply, {"reply": "reply-for-req-1"})


class KillPointMatrixTests(unittest.TestCase):
    """provider/tool/delivery/shell rows of the fault matrix."""

    def test_provider_before_send_leaves_no_effect_row(self) -> None:
        world = _world()
        try:
            world.accept_operation("run-1")
            # Timeout before begin: the intent is never committed.
            world.provider.mode = "timeout_before_execute"
            with self.assertRaises(FakeProviderTimeout):
                world.provider.send({"id": "req-1"})
            oracle = InvariantChecker(seed=11)
            facts = oracle.check_recovery_idempotent(world.restart().canonical)
            self.assertEqual(world.pending_provider_ids("run-1"), ())
            self.assertEqual(
                [r for r in facts["log_rows"] if r["record"] == "intent"], []
            )
            oracle.assert_valid(facts)
        finally:
            world.keepalive.cleanup()

    def test_provider_after_send_stays_pending_unknown(self) -> None:
        world = _world()
        try:
            world.accept_operation("run-1")
            intent = world.provider_intent("run-1")
            world.begin_provider("run-1", intent)
            # Timeout after the provider executed: kill before any settlement.
            world.restart()
            oracle = InvariantChecker(seed=11)
            facts = oracle.check_recovery_idempotent(world.canonical)
            self.assertEqual(world.pending_provider_ids("run-1"), (intent.effect_id,))
            oracle.assert_valid(facts, unknowns=[(intent.effect_id, "pending")])
        finally:
            world.keepalive.cleanup()

    def test_provider_before_settle_settles_exactly_once(self) -> None:
        world = _world()
        try:
            world.accept_operation("run-1")
            intent = world.provider_intent("run-1")
            world.begin_provider("run-1", intent)
            world.settle_provider("run-1", intent.effect_id)
            world.restart()
            oracle = InvariantChecker(seed=11)
            facts = oracle.check_recovery_idempotent(world.canonical)
            self.assertEqual(world.pending_provider_ids("run-1"), ())
            intents = [
                r for r in facts["log_rows"]
                if r["record"] == "intent" and r["effect_id"] == intent.effect_id
            ]
            self.assertEqual(len(intents), 1)
            oracle.assert_valid(facts)
        finally:
            world.keepalive.cleanup()

    def test_rebegin_same_effect_fails_closed_without_duplicate(self) -> None:
        world = _world()
        try:
            world.accept_operation("run-1")
            intent = world.provider_intent("run-1")
            world.begin_provider("run-1", intent)
            with self.assertRaises(RuntimeEffectError):
                world.begin_provider("run-1", intent)
            world.restart()
            oracle = InvariantChecker(seed=11)
            facts = oracle.check_recovery_idempotent(world.canonical)
            intents = [
                r for r in facts["log_rows"]
                if r["record"] == "intent" and r["effect_id"] == intent.effect_id
            ]
            self.assertEqual(len(intents), 1)
            oracle.assert_valid(facts)
        finally:
            world.keepalive.cleanup()

    def test_tool_before_execute_replays_safe_only(self) -> None:
        world = _world()
        try:
            world.accept_operation("run-1")
            batch = world.tool_batch_intent("run-1", 1, ("ref-a",))
            tool = world.tool_intent("run-1", "ref-a")
            world.begin_batch("run-1", batch, tool)
            world.restart()
            oracle = InvariantChecker(seed=11)
            facts = oracle.check_recovery_idempotent(world.canonical)
            undelivered = world.undelivered_batches("run-1")
            self.assertEqual([b.intent.batch_id for b in undelivered], [batch.batch_id])
            oracle.assert_valid(facts)
        finally:
            world.keepalive.cleanup()

    def test_shell_before_popen_rejected_zero_spawns(self) -> None:
        world = _world()
        try:
            approval_id = world.approve("echo hi")
            expired = world.approvals.expire_shell_results(run_id="run-1")
            self.assertEqual(len(expired), 1)
            # The popped approval is gone: nothing left to execute.
            self.assertIsNone(world.approvals.pop_shell(approval_id))
            self.assertEqual(world.approvals.shell_snapshot(), {})
            oracle = InvariantChecker(seed=11)
            oracle.assert_valid(normalize(world.canonical()))
        finally:
            world.keepalive.cleanup()

    def test_shell_after_claim_cannot_execute_twice(self) -> None:
        world = _world()
        try:
            approval_id = world.approve("echo hi")
            first = world.approvals.pop_shell(approval_id)
            self.assertIsNotNone(first)
            # Ticket consumed: a second claim for the same card is gone.
            self.assertIsNone(world.approvals.pop_shell(approval_id))
            oracle = InvariantChecker(seed=11)
            oracle.assert_valid(normalize(world.canonical()))
        finally:
            world.keepalive.cleanup()


if __name__ == "__main__":
    unittest.main()
