"""P4: 1000 seeded mixed operations with faults, kills, and restarts.

One deterministic simulation drives provider/tool/ghost/sse/approval
operations against a single world while the seeded FaultController injects
timeout / duplicate / kill faults. Kills drop all memory handles and reopen
the same state directory. The end state must satisfy every durable
invariant, recovery must be idempotent, and a second run of the same seed
must produce byte-identical canonical facts.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.runtime.effects.effect_records import RuntimeEffectError
from tests.stress.model import ExpectedOutcome, canonical_json
from tests.stress.oracle import InvariantChecker
from tests.stress.world import FakeProviderTimeout, StressWorld

OPERATIONS = 1000
KILL_EVERY = 100


def run_simulation(seed: int, operations: int = OPERATIONS) -> dict:
    """Drive one full mixed workload; return the debrief for the oracle.

    Every operation gets a fresh run (accept + mark_writer_running), so each
    op exercises exactly one phase-legal step and the operation state machine
    never couples unrelated ops.
    """
    tmp = tempfile.TemporaryDirectory()
    world = StressWorld(Path(tmp.name) / "state", seed=seed)
    committed: list[str] = []
    rejected: list[str] = []
    unknowns: list[tuple[str, str]] = []
    terminal: dict[str, str] = {}
    per_run_committed: dict[str, list[str]] = {}
    sse_seq = 0

    def _commit(run_id: str, fact_id: str) -> None:
        committed.append(fact_id)
        per_run_committed.setdefault(run_id, []).append(fact_id)

    try:
        for index in range(operations):
            run_id = f"run-{index:04d}"
            world.accept_operation(run_id)
            kind = world.faults.choose("op", ("provider", "tool", "ghost", "sse", "approval"))
            if kind == "provider":
                intent = world.provider_intent(run_id)
                mode = world.faults.choose(
                    "provider-mode", ("execute_then_timeout", "timeout_before_execute", "ok")
                )
                world.provider.mode = mode
                try:
                    world.provider.send({"id": intent.effect_id})
                except FakeProviderTimeout:
                    if mode == "timeout_before_execute":
                        rejected.append(intent.effect_id)
                        terminal[run_id] = "timeout-before-send"
                        continue
                    # Executed but unconfirmed: commit the intent, never settle.
                    world.begin_provider(run_id, intent)
                    _commit(run_id, intent.effect_id)
                    unknowns.append((intent.effect_id, "pending"))
                    terminal[run_id] = "unknown"
                    continue
                world.begin_provider(run_id, intent)
                _commit(run_id, intent.effect_id)
                if world.faults.should_timeout("settle", probability=0.3):
                    unknowns.append((intent.effect_id, "pending"))
                    terminal[run_id] = "unknown"
                else:
                    world.settle_provider(run_id, intent.effect_id)
                    terminal[run_id] = "settled"
            elif kind == "tool":
                batch = world.tool_batch_intent(run_id, 1, (f"ref-{index}",))
                tool = world.tool_intent(run_id, f"ref-{index}")
                world.begin_batch(run_id, batch, tool)
                _commit(run_id, batch.batch_id)
                if world.faults.duplicate_count("batch", maximum=1):
                    try:
                        world.begin_batch(run_id, batch, tool)
                    except RuntimeEffectError:
                        rejected.append(batch.batch_id + ":dup")
                terminal[run_id] = "batched"
            elif kind == "ghost":
                world.ghost_append(1 + index % 3, tag=f"op{index}")
                terminal[run_id] = "ghost-appended"
            elif kind == "sse":
                sse_seq += 1
                world.sse_emit(f"e{sse_seq}", type="tool", n=sse_seq)
                for _ in range(world.faults.duplicate_count("sse", maximum=2)):
                    world.sse_emit(f"e{sse_seq}", type="tool", n=sse_seq)
                terminal[run_id] = "emitted"
            else:
                approval_id = world.approve(f"cmd-{index}", run_id=run_id)
                _commit(run_id, approval_id)
                if world.faults.coin_flip("expire"):
                    world.approvals.expire_shell_results(run_id=run_id)
                    terminal[run_id] = "approval-expired"
                else:
                    terminal[run_id] = "approval-pending"
            if (index + 1) % KILL_EVERY == 0:
                world.restart()
        world.restart()
        oracle = InvariantChecker(seed=seed)
        facts = oracle.check_recovery_idempotent(world.canonical)
        oracle.assert_valid(facts, unknowns=unknowns)
        oracle.check_no_duplicate_facts(
            [c for c in committed if not c.endswith(":dup")]
        )
        return {
            "facts": facts,
            "canonical": canonical_json(facts),
            "terminal": terminal,
            "committed": committed,
            "rejected": rejected,
            "per_run_committed": per_run_committed,
        }
    finally:
        tmp.cleanup()


class MixedWorkloadTests(unittest.TestCase):
    def test_1000_mixed_operations_converge(self) -> None:
        debrief = run_simulation(0xC0DE)
        self.assertGreater(len(debrief["committed"]), 500)
        self.assertGreater(len(debrief["terminal"]), 0)

    def test_same_seed_replays_identical_facts(self) -> None:
        first = run_simulation(0xC0DE, operations=200)
        second = run_simulation(0xC0DE, operations=200)
        self.assertEqual(first["canonical"], second["canonical"])

    def test_expected_outcomes_match_terminal_states(self) -> None:
        debrief = run_simulation(99, operations=200)
        outcomes = [
            ExpectedOutcome(
                operation_id=run_id,
                terminal_state=state,
                committed_effects=tuple(debrief["per_run_committed"].get(run_id, [])),
            )
            for run_id, state in sorted(debrief["terminal"].items())
        ]
        states = {o.terminal_state for o in outcomes}
        self.assertTrue(
            states
            <= {
                "settled", "unknown", "batched", "timeout-before-send",
                "ghost-appended", "emitted", "approval-pending", "approval-expired",
            }
        )
        self.assertGreater(len(outcomes), 0)
        # Every committed id of a run is attached to that run's outcome.
        flat = [c for ids in debrief["per_run_committed"].values() for c in ids]
        self.assertEqual(sorted(flat), sorted(debrief["committed"]))


if __name__ == "__main__":
    unittest.main()
