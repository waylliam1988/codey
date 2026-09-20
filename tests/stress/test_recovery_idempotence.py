"""P0: recovery idempotence -- R(R(S)) == R(S) on every durable surface."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.stress.model import canonical_json, normalize
from tests.stress.oracle import InvariantChecker
from tests.stress.world import StressWorld


def _world(seed: int = 7):
    tmp = tempfile.TemporaryDirectory()
    world = StressWorld(Path(tmp.name) / "state", seed=seed)
    world.keepalive = tmp
    return world


class RecoveryIdempotenceTests(unittest.TestCase):
    def test_empty_world_recovers_to_empty(self) -> None:
        world = _world()
        oracle = InvariantChecker(seed=7)
        facts = oracle.check_recovery_idempotent(world.restart().canonical)
        oracle.assert_valid(facts)
        world.keepalive.cleanup()

    def test_accepted_operation_survives_restart(self) -> None:
        world = _world()
        world.accept_operation("run-1")
        oracle = InvariantChecker(seed=7)
        before = normalize(world.canonical())
        world.restart()
        facts = oracle.check_recovery_idempotent(world.canonical)
        self.assertEqual(normalize(facts), before)
        oracle.check_no_new_operations_on_restart(
            [r["operation_id"] for r in facts["log_rows"]],
            [r["operation_id"] for r in before["log_rows"]],
        )
        world.keepalive.cleanup()

    def test_pending_provider_intent_stays_unknown_across_restart(self) -> None:
        world = _world()
        world.accept_operation("run-1")
        intent = world.provider_intent("run-1")
        world.begin_provider("run-1", intent)
        oracle = InvariantChecker(seed=7)
        facts = oracle.check_recovery_idempotent(world.restart().canonical)
        pending = world.pending_provider_ids("run-1")
        self.assertEqual(pending, (intent.effect_id,))
        oracle.assert_valid(facts, unknowns=[(intent.effect_id, "pending")])
        world.keepalive.cleanup()

    def test_tool_batch_and_delivery_survive_restart(self) -> None:
        world = _world()
        world.accept_operation("run-1")
        batch = world.tool_batch_intent("run-1", 1, ("ref-a", "ref-b"))
        tool = world.tool_intent("run-1", "ref-a")
        world.begin_batch("run-1", batch, tool)
        oracle = InvariantChecker(seed=7)
        facts = oracle.check_recovery_idempotent(world.restart().canonical)
        undelivered = world.undelivered_batches("run-1")
        self.assertEqual(len(undelivered), 1)
        self.assertEqual(undelivered[0].intent.batch_id, batch.batch_id)
        oracle.assert_valid(facts)
        world.keepalive.cleanup()

    def test_torn_tail_repairs_to_same_facts(self) -> None:
        world = _world()
        world.accept_operation("run-1")
        world.ghost_append(3)
        path = world.log.path_for(world.session_id)
        with path.open("ab") as handle:
            handle.write(b'{"torn": "tail", "unterminated": ')
        oracle = InvariantChecker(seed=7)
        first = world.restart().canonical()
        second = world.canonical()
        self.assertEqual(normalize(first), normalize(second))
        oracle.assert_valid(normalize(second))
        world.keepalive.cleanup()

    def test_ephemeral_surfaces_rebuild_empty(self) -> None:
        world = _world()
        world.sse_emit("e1", type="tool")
        world.approve("echo hi")
        self.assertEqual(len(world.bus.replay_events_after(0)), 1)
        world.restart()
        self.assertEqual(world.bus.replay_events_after(0), [])
        self.assertEqual(world.approvals.shell_snapshot(), {})
        facts = world.canonical()
        self.assertEqual(facts["approvals"], [])
        world.keepalive.cleanup()

    def test_canonical_json_is_stable(self) -> None:
        world = _world()
        world.accept_operation("run-1")
        world.ghost_append(2)
        once = canonical_json(world.canonical())
        world.restart()
        twice = canonical_json(world.canonical())
        self.assertEqual(once, twice)
        world.keepalive.cleanup()


if __name__ == "__main__":
    unittest.main()
