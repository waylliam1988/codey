"""Real process kill -> restart -> durable recovery.

Unlike the logical-kill scenarios, these tests spawn a real child process,
terminate it with ``Popen.kill()`` (TerminateProcess on Windows, SIGKILL on
POSIX) while it idles at a checkpoint, then respawn it in recover mode. The
oracle compares canonical durable facts: kill must be indistinguishable
from a clean exit, and double recovery must be idempotent.

Scope: the kill always lands at an IDLE checkpoint, so everything asserted
here was already fsynced and acknowledged. That establishes Real Process
Crash Recovery, not Crash Safety -- kills inside a write (torn rows,
partial batches, pre/post rename, torn journal tails) live in
``test_crash_point_matrix``.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from tests.stress.model import normalize
from tests.stress.oracle import InvariantChecker
from tests.stress.process import (
    _kill,
    _read_line,
    _recover_canonical,
    _spawn,
    _wait_file,
)


class ProcKillRecoveryTests(unittest.TestCase):
    def test_real_kill_recovers_pending_provider_intent(self) -> None:
        oracle = InvariantChecker(seed=41)
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "state"
            checkpoint = Path(td) / "ready"
            steps = [
                {"do": "accept", "run": "run-1"},
                {"do": "begin_provider", "run": "run-1"},
                {"do": "checkpoint", "file": str(checkpoint)},
            ]
            proc = _spawn("write", home, steps)
            try:
                _wait_file(checkpoint)
                manifest = _read_line(proc)["manifest"]
                time.sleep(0.5)
                _kill(proc)
            finally:
                if proc.poll() is None:
                    proc.kill()
            self.assertEqual(len(manifest["effects"]), 1)
            first = _recover_canonical(home)
            intents = [
                row for row in first["log_rows"]
                if row.get("record") == "intent"
                and row.get("effect_id") == manifest["effects"][0]
            ]
            self.assertEqual(len(intents), 1)
            second = _recover_canonical(home)
            oracle.check_recovery_idempotent(lambda: second)
            self.assertEqual(first, second)
            oracle.assert_valid(first)

    def test_real_kill_recovers_ghost_and_tool_batch(self) -> None:
        oracle = InvariantChecker(seed=42)
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "state"
            checkpoint = Path(td) / "ready"
            steps = [
                {"do": "accept", "run": "run-2"},
                {"do": "tool_batch", "run": "run-2", "refs": ["ref-kill"]},
                {"do": "ghost", "n": 5, "tag": "kill"},
                {"do": "checkpoint", "file": str(checkpoint)},
            ]
            proc = _spawn("write", home, steps)
            try:
                _wait_file(checkpoint)
                manifest = _read_line(proc)["manifest"]
                time.sleep(0.5)
                _kill(proc)
            finally:
                if proc.poll() is None:
                    proc.kill()
            self.assertEqual(manifest["ghosts"], 5)
            self.assertEqual(len(manifest["batches"]), 1)
            facts = _recover_canonical(home)
            self.assertEqual(len(facts["ghost_rows"]), 5)
            batch_ids = [b["batch_id"] for b in facts["delivery_batches"]]
            self.assertIn(manifest["batches"][0], batch_ids)
            oracle.assert_valid(facts)

    def test_kill_matches_uninterrupted_execution(self) -> None:
        from tests.stress.world import StressWorld

        with tempfile.TemporaryDirectory() as td:
            killed_home = Path(td) / "killed"
            checkpoint = Path(td) / "ready"
            steps = [
                {"do": "accept", "run": "run-9"},
                {"do": "begin_provider", "run": "run-9"},
                {"do": "ghost", "n": 2, "tag": "cmp"},
                {"do": "checkpoint", "file": str(checkpoint)},
            ]
            proc = _spawn("write", killed_home, steps)
            try:
                _wait_file(checkpoint)
                manifest = _read_line(proc)["manifest"]
                time.sleep(0.5)
                _kill(proc)
            finally:
                if proc.poll() is None:
                    proc.kill()
            killed = _recover_canonical(killed_home)
            # Same steps without any kill, built in-process.
            clean_tmp = tempfile.TemporaryDirectory()
            try:
                clean = StressWorld(Path(clean_tmp.name) / "state", seed=0)
                clean.accept_operation("run-9")
                intent = clean.provider_intent("run-9")
                clean.begin_provider("run-9", intent)
                clean.ghost_append(2, tag="cmp")
                clean_facts = normalize(clean.canonical())
            finally:
                clean_tmp.cleanup()
            self.assertEqual(
                sorted(r["record"] for r in clean_facts["log_rows"]),
                sorted(r["record"] for r in killed["log_rows"]),
            )
            self.assertEqual(len(killed["ghost_rows"]), 2)
            self.assertEqual(
                [r["effect_id"] for r in killed["log_rows"] if r["record"] == "intent"],
                manifest["effects"],
            )


if __name__ == "__main__":
    unittest.main()
