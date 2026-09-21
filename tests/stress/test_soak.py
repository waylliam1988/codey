"""Soak regression tests: determinism, shrink, and a gate-safe mini soak.

The long tiers (nightly 10k, weekly 100k) run via the CLI, not here::

    python -m tests.stress.soak --tier nightly
    python -m tests.stress.soak --seed 827361 --operations 100000

What pytest pins: the same seed replays byte-identical facts, the shrink
machinery reduces a failing script on real replay, and a small soak
converges under the oracle. If the CLI ever reports a failure, the
artifact it saves becomes a new test here (script replay, not the full
soak).
"""

from __future__ import annotations

import unittest

from tests.stress.model import canonical_json
from tests.stress.scheduler import RESTART, SoakFailure, replay_script
from tests.stress.shrink import ddmin, shrink_script
from tests.stress.soak import run_soak


def _failing_script() -> list[dict]:
    """A script that fails deterministically: settle for an unknown intent."""
    return [
        {"op": "accept", "run": "soak-000001", "faults": []},
        {"op": "ghost_append", "n": 2, "tag": "fill1", "faults": []},
        {"op": "ghost_append", "n": 1, "tag": "fill2", "faults": []},
        {
            "op": "provider_settle", "run": "soak-000001",
            "effect": "effect-prov-999", "status": "ok", "error_code": "",
            "dup": False, "faults": [],
        },
        {"op": "ghost_append", "n": 1, "tag": "fill3", "faults": []},
    ]


class SoakDeterminismTests(unittest.TestCase):
    def test_same_seed_replays_identical_facts(self) -> None:
        first = run_soak(0x50A1, 150, check_every=50, collect_script=False)
        second = run_soak(0x50A1, 150, check_every=50, collect_script=False)
        self.assertEqual(first["canonical"], second["canonical"])

    def test_recorded_script_replays_identical_facts(self) -> None:
        import tempfile
        from pathlib import Path

        debrief = run_soak(0x50A2, 150, check_every=0)
        with tempfile.TemporaryDirectory() as td:
            replayed = replay_script(debrief["script"], Path(td) / "state")
        self.assertEqual(
            canonical_json(debrief["facts"]), canonical_json(replayed["facts"])
        )
        # The script exercises restarts, not just straight-line ops.
        kinds = {step["op"] for step in debrief["script"]}
        self.assertIn(RESTART, kinds)


class ShrinkTests(unittest.TestCase):
    def test_ddmin_keeps_only_essential_steps(self) -> None:
        script = [{"op": f"fill-{index}"} for index in range(20)]
        script.insert(7, {"op": "need-a"})
        script.insert(15, {"op": "need-b"})

        def _needs_both(candidate: list[dict]) -> bool:
            ops = [step["op"] for step in candidate]
            return "need-a" in ops and "need-b" in ops

        shrunk = ddmin(script, _needs_both)
        self.assertEqual([step["op"] for step in shrunk], ["need-a", "need-b"])

    def test_shrink_reduces_failing_script_on_real_replay(self) -> None:
        import tempfile
        from pathlib import Path

        script = _failing_script()
        with tempfile.TemporaryDirectory() as td:
            try:
                replay_script(script, Path(td) / "state")
            except Exception as exc:
                failure = exc if isinstance(exc, SoakFailure) else SoakFailure(3, script[3], exc)
            else:
                self.fail("script should fail")
        shrunk = shrink_script(script, failure, budget_s=120.0)
        self.assertLess(len(shrunk), len(script))
        self.assertEqual(shrunk[-1]["op"], "provider_settle")
        # The reduced script still reproduces on real replay.
        with tempfile.TemporaryDirectory() as td, self.assertRaises(SoakFailure):
            replay_script(shrunk, Path(td) / "state")


class MiniSoakTests(unittest.TestCase):
    def test_300_operations_converge(self) -> None:
        debrief = run_soak(0x50A3, 300, check_every=100)
        self.assertGreater(len(debrief["committed"]), 50)
        self.assertGreater(debrief["restarts"], 0)
        self.assertGreater(sum(debrief["fault_counts"].values()), 0)
        kinds = {step["op"] for step in debrief["script"]}
        self.assertTrue({"provider_send", "tool_begin", "ghost_append"} <= kinds)


if __name__ == "__main__":
    unittest.main()
