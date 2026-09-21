"""P2: shell Stop/Allow race -- exactly one linearization, never Stop-then-spawn.

The spawn gate + approval generation must linearize every race into either
"Stop won" (zero spawns) or "Allow won" (exactly one spawn). A spawn landing
after a completed Stop is the one forbidden outcome. Spawns are faked
(mocked Popen); what is tested is the order, not the OS.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from unittest import mock

from codey.app import shell_service
from codey.app.approval_registry import ApprovalRegistry
from codey.runtime.core import cancellation
from tests.stress.oracle import InvariantChecker
from tests.stress.scheduler import counting_spawn, shell_ctx


def _ctx(approvals: ApprovalRegistry):
    # Canonical shell harness lives in scheduler.py (shared with soak);
    # this test only adapts the pending-request shape.
    return shell_ctx(approvals)


def _pending(project: str) -> dict:
    return {
        "id": "shell-race",
        "run_id": "run-1",
        "session_id": "sess-1",
        "command": "echo hi",
        "cwd": ".",
        "project": project,
    }


def _fake_spawn_counter(spawns: list, order: list):
    return counting_spawn(spawns, order, stamp=time.perf_counter_ns)


class ShellRaceTests(unittest.TestCase):
    def test_stop_then_claim_mints_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            approvals = ApprovalRegistry()
            approvals.add_shell("shell-race", _pending(td))
            approvals.expire_shell_results()
            ctx = _ctx(approvals)
            pending, ticket = shell_service.claim_shell_ticket(
                ctx, "shell-race", timeout=30, output_limit=1000
            )
            self.assertIsNone(pending)
            self.assertIsNone(ticket)

    def test_claim_then_execute_without_stop_spawns_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            approvals = ApprovalRegistry()
            approvals.add_shell("shell-race", _pending(td))
            ctx = _ctx(approvals)
            spawns: list[float] = []
            start, wait = _fake_spawn_counter(spawns, [])
            with (
                mock.patch.object(cancellation, "start_process", side_effect=start),
                mock.patch.object(cancellation, "wait_process", side_effect=wait),
            ):
                pending, ticket = shell_service.claim_shell_ticket(
                    ctx, "shell-race", timeout=30, output_limit=1000
                )
                self.assertIsNotNone(ticket)
                result = shell_service.execute_shell_ticket(ctx, ticket)
            self.assertTrue(result["ok"])
            self.assertEqual(len(spawns), 1)

    def _run_one_race(self, td: str, oracle: InvariantChecker, jitter) -> bool:
        """One Allow-vs-Stop interleaving; returns True when Allow spawned."""
        approvals = ApprovalRegistry()
        approvals.add_shell("shell-race", _pending(td))
        ctx = _ctx(approvals)
        spawns: list[float] = []
        stop_done: list[float] = []
        order: list[str] = []
        barrier = threading.Barrier(2)
        start, wait = _fake_spawn_counter(spawns, order)

        def _allow() -> None:
            barrier.wait(timeout=5)
            time.sleep(jitter.random() * 0.001)
            _, ticket = shell_service.claim_shell_ticket(
                ctx, "shell-race", timeout=30, output_limit=1000
            )
            if ticket is None:
                return
            with (
                mock.patch.object(cancellation, "start_process", side_effect=start),
                mock.patch.object(cancellation, "wait_process", side_effect=wait),
            ):
                shell_service.execute_shell_ticket(ctx, ticket)

        def _stop() -> None:
            barrier.wait(timeout=5)
            time.sleep(jitter.random() * 0.001)
            with ctx._shell_spawn_gate:
                ctx.run_registry.stop_flag.set()
                ctx.approvals.expire_shell_results()
            stop_done.append(time.perf_counter_ns())
            order.append("stop")

        first = threading.Thread(target=_allow, daemon=True)
        second = threading.Thread(target=_stop, daemon=True)
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        oracle.check_stop_allow_linearized(spawns, stop_done[0] if stop_done else None)
        if "stop" in order:
            self.assertNotIn("spawn", order[order.index("stop") + 1:])
        return bool(spawns)

    def test_allow_stop_race_never_spawns_after_stop(self) -> None:
        import random

        oracle = InvariantChecker(seed=21)
        jitter = random.Random(21)
        stop_first = 0
        allow_first = 0
        with tempfile.TemporaryDirectory() as td:
            for _ in range(1000):
                if self._run_one_race(td, oracle, jitter):
                    allow_first += 1
                else:
                    stop_first += 1
        # The race is real only if both linearizations were observed.
        self.assertGreater(stop_first, 0)
        self.assertGreater(allow_first, 0)


if __name__ == "__main__":
    unittest.main()
