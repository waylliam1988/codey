"""Same batch id with different content is rejected loud, never merged.

Found while building the soak's duplicate-retry fault: resending the
byte-identical batch is a clean duplicate rejection, but the same id with
a different ref (different digest) raises ``RuntimeOperationTransitionError
("delivery batch intent conflict")``. The dangerous direction would be
silently accepting it as a second fact; this test pins the loud rejection
and that no new batch is committed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.runtime.core.operation_state import RuntimeOperationTransitionError
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    compute_batch_digest,
)
from tests.stress.world import StressWorld


class ConflictingBatchTests(unittest.TestCase):
    def test_conflicting_batch_id_raises_and_commits_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            world = StressWorld(Path(td) / "state", seed=0)
            world.accept_operation("run-conflict")
            batch = world.tool_batch_intent("run-conflict", 1, ("ref-a",))
            tool = world.tool_intent("run-conflict", "ref-a")
            world.begin_batch("run-conflict", batch, tool)

            items = (
                DeliveryBatchItem(tool_index=0, tool_name="read", ref="ref-B", replay_class="safe"),
            )
            forged = DeliveryBatchIntent(
                batch_id=batch.batch_id,
                session_id=world.session_id,
                run_id="run-conflict",
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            )
            fresh_tool = world.tool_intent("run-conflict", "ref-B")
            with self.assertRaises(RuntimeOperationTransitionError):
                world.begin_batch("run-conflict", forged, fresh_tool)
            batches = world.delivery.load_batches(world.session_id, "run-conflict")
            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0].intent.batch_id, batch.batch_id)


if __name__ == "__main__":
    unittest.main()
