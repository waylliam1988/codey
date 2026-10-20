"""P3: ghost replay -- the projection is a pure function of durable facts.

Append 1..4, kill, rebuild from the log, replay 2+3, then rebuild from
scratch: the fold must be identical at every step. A replayed row must
never double-apply downstream.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.stress.model import fold_event_rows, normalize
from tests.stress.oracle import InvariantChecker
from tests.stress.world import StressWorld


def _world(seed: int = 31):
    tmp = tempfile.TemporaryDirectory()
    world = StressWorld(Path(tmp.name) / "state", seed=seed)
    world.keepalive = tmp
    return world


def _fold(rows: tuple) -> tuple:
    return fold_event_rows(rows)


class GhostReplayTests(unittest.TestCase):
    def test_kill_rebuilds_same_rows(self) -> None:
        world = _world()
        try:
            world.ghost_append(4)
            before = world.ghost_rows()
            self.assertEqual(len(before), 4)
            world.restart()
            oracle = InvariantChecker(seed=31)
            facts = oracle.check_recovery_idempotent(world.canonical)
            self.assertEqual(world.ghost_rows(), before)
            oracle.assert_valid(facts)
        finally:
            world.keepalive.cleanup()

    def test_replayed_rows_do_not_double_apply(self) -> None:
        world = _world()
        try:
            world.ghost_append(4)
            base = world.ghost_rows()
            replayed = (base[1], base[2], base[1], base[2], base[2])
            with world.ghost.transaction():
                world.ghost.append(list(replayed))
                world.ghost.mark_applied()
            oracle = InvariantChecker(seed=31)
            oracle.check_replay_idempotent(_fold, base, replayed)
            world.restart()
            oracle.check_replay_idempotent(_fold, base, tuple(world.ghost_rows()[4:]))
        finally:
            world.keepalive.cleanup()

    def test_atomic_rewrite_equals_incremental_append(self) -> None:
        world = _world()
        try:
            world.ghost_append(4)
            incremental = world.ghost_rows()
            world.restart()
            oracle = InvariantChecker(seed=31)
            rebuilt = world.ghost_rows()
            oracle.check_projection_rebuildable(
                _fold(incremental), _fold(rebuilt)
            )
            # Full rewrite of the same rows converges to the same fold.
            with world.ghost.transaction():
                world.ghost.append(list(rebuilt), replace=True)
                world.ghost.mark_applied()
            oracle.check_projection_rebuildable(
                _fold(incremental), _fold(world.ghost_rows())
            )
            oracle.assert_valid(normalize(world.canonical()))
        finally:
            world.keepalive.cleanup()

    def test_session_projection_incremental_equals_rebuild(self) -> None:
        world = _world()
        try:
            world.accept_operation("run-1")
            intent = world.provider_intent("run-1")
            world.begin_provider("run-1", intent)
            world.settle_provider("run-1", intent.effect_id)
            state = world.operations.load(world.session_id, "run-1")
            self.assertIsNotNone(state)
            incremental = {
                "operation": state.to_payload(),
                "effects": [
                    {
                        "intent": effect.intent.to_payload(),
                        "settlement": effect.settlement.to_payload() if effect.settlement else None,
                    }
                    for effect in world.effects.load_effects(world.session_id, "run-1")
                ],
                "batches": [batch.intent.to_payload() for batch in world.delivery.load_batches(world.session_id, "run-1")],
            }
            world.restart()
            rebuilt_state = world.operations.load(world.session_id, "run-1")
            self.assertIsNotNone(rebuilt_state)
            rebuilt = {
                "operation": rebuilt_state.to_payload(),
                "effects": [
                    {
                        "intent": effect.intent.to_payload(),
                        "settlement": effect.settlement.to_payload() if effect.settlement else None,
                    }
                    for effect in world.effects.load_effects(world.session_id, "run-1")
                ],
                "batches": [batch.intent.to_payload() for batch in world.delivery.load_batches(world.session_id, "run-1")],
            }
            oracle = InvariantChecker(seed=31)
            oracle.check_projection_rebuildable(incremental, rebuilt)
        finally:
            world.keepalive.cleanup()


if __name__ == "__main__":
    unittest.main()
