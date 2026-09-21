"""Crash-point matrix: kill at every internal durable-write critical point.

``test_proc_kill_recovery`` kills an IDLE child: everything it asserts was
already fsynced and acknowledged before the kill. That establishes Real
Process Crash Recovery, not Crash Safety. This matrix covers the points
that test cannot reach:

  session log append:   torn row mid-batch ············· kill here
  session log append:   full batch, fsync done, no ack · kill here
  ghost log append:     2 of 5 chunks on disk ··········· kill here
  ghost log append:     torn row mid-batch ·············· kill here
  atomic replace:       tmp fsynced, rename blocked ····· kill here (+ orphan tmp)
  atomic replace:       rename done, caller dead ········· kill here
  repair journal:       torn tail mid-flush ············· kill here

Honesty notes (read before "optimizing" this file):

1. A parent-side kill can never land between two syscalls deterministically,
   so the points are CONSTRUCTED, not timed: the child drives the real
   production write path through a test-only fault hook that stops at the
   critical point (prefix bytes already in the page cache), signals a
   rendezvous file, then idles. The kill itself is real (``Popen.kill()``:
   memory, fds and OS locks are gone) and recovery runs in a fresh
   process. No production code carries crash hooks.
2. Process kill is not power loss: the page cache survives, so
   flush-vs-fsync orderings are vacuous here and deliberately untested.
   What the matrix proves is byte-prefix and rename safety: torn tails are
   repaired or skipped, pre-rename originals stay intact, orphan tmps are
   ignored, and full-but-unacknowledged writes stay durable.
3. The repair journal has no fsync by design (audit trail, production
   write-only): a kill can only lose its tail, never corrupt earlier
   facts. Its matrix point pins the reader side: recovery must not crash
   on a torn journal tail.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.stress.oracle import InvariantChecker
from tests.stress.test_proc_kill_recovery import (
    WORKER,
    _child_env,
    _kill,
    _read_line,
    _recover_canonical,
    _wait_file,
)


def _spawn_crashpoint(point: str, state_home: Path, rendezvous: Path):
    return subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(WORKER),
            "--mode",
            "crashpoint",
            "--state-home",
            str(state_home),
            "--point",
            point,
            "--rendezvous",
            str(rendezvous),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(),
    )


def _intent_ids(facts: dict) -> list[str]:
    return [
        str(row.get("effect_id", ""))
        for row in facts["log_rows"]
        if row.get("record") == "intent"
    ]


def _ghost_ids(facts: dict) -> list[str]:
    return [str(row.get("id", "")) for row in facts["ghost_rows"]]


class CrashPointMatrixTests(unittest.TestCase):
    def _drive(self, point: str, td: str) -> tuple[Path, dict, dict]:
        home = Path(td) / "state"
        rendezvous = Path(td) / "ready"
        proc = _spawn_crashpoint(point, home, rendezvous)
        try:
            _wait_file(rendezvous)
            manifest = _read_line(proc)["manifest"]
            time.sleep(0.5)
            _kill(proc)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(manifest["point"], point)
        return home, manifest, _recover_canonical(home)

    def _check_converges(self, oracle: InvariantChecker, home: Path, facts: dict) -> None:
        oracle.assert_valid(facts)
        second = oracle.check_recovery_idempotent(lambda: _recover_canonical(home))
        self.assertEqual(facts, second)

    def test_session_torn_row_is_repaired_and_baseline_survives(self) -> None:
        oracle = InvariantChecker(seed=101)
        with tempfile.TemporaryDirectory() as td:
            home, manifest, facts = self._drive("session-torn-row", td)
            self.assertEqual(
                _intent_ids(facts),
                [manifest["baseline_effect"]],
                "torn intent must vanish, committed intent must survive",
            )
            self.assertNotIn(manifest["crashed_effect"], _intent_ids(facts))
            self._check_converges(oracle, home, facts)

    def test_session_full_write_without_ack_stays_durable(self) -> None:
        oracle = InvariantChecker(seed=102)
        with tempfile.TemporaryDirectory() as td:
            home, manifest, facts = self._drive("session-full-no-ack", td)
            self.assertEqual(
                _intent_ids(facts),
                [manifest["baseline_effect"], manifest["crashed_effect"]],
            )
            self._check_converges(oracle, home, facts)

    def test_ghost_partial_batch_leaves_exact_prefix(self) -> None:
        oracle = InvariantChecker(seed=103)
        with tempfile.TemporaryDirectory() as td:
            home, manifest, facts = self._drive("ghost-partial-batch", td)
            self.assertEqual(_ghost_ids(facts), manifest["ghost_ids"][: manifest["durable_count"]])
            self.assertEqual(
                _intent_ids(facts),
                [manifest["baseline_effect"]],
                "session surface must be untouched by a ghost crash",
            )
            self._check_converges(oracle, home, facts)

    def test_ghost_torn_row_skips_bad_tail_and_keeps_good_rows(self) -> None:
        oracle = InvariantChecker(seed=104)
        with tempfile.TemporaryDirectory() as td:
            home, manifest, facts = self._drive("ghost-torn-row", td)
            self.assertEqual(_ghost_ids(facts), manifest["ghost_ids"][:1])
            self._check_converges(oracle, home, facts)

    def test_primitive_replace_pre_rename_keeps_original_and_ignores_orphan_tmp(self) -> None:
        from tests.stress.world import StressWorld

        oracle = InvariantChecker(seed=105)
        with tempfile.TemporaryDirectory() as td:
            home, manifest, facts = self._drive("primitive-replace-pre-rename", td)
            target = Path(manifest["target"])
            self.assertEqual(target.read_text(encoding="utf-8"), manifest["want_content"])
            orphans = sorted(target.parent.glob(manifest["tmp_glob"]))
            self.assertEqual(len(orphans), 1, f"expected the orphan tmp, found: {orphans}")
            self._check_converges(oracle, home, facts)
            # The orphan must not block future writes or locks.
            world = StressWorld(home, seed=0)
            world.ghost_append(1, tag="after")
            self.assertIn("ghost-after-1", [row["id"] for row in world.ghost_rows()])

    def test_primitive_replace_post_rename_keeps_new_content(self) -> None:
        oracle = InvariantChecker(seed=106)
        with tempfile.TemporaryDirectory() as td:
            home, manifest, facts = self._drive("primitive-replace-post-rename", td)
            target = Path(manifest["target"])
            self.assertEqual(target.read_text(encoding="utf-8"), manifest["want_content"])
            self.assertEqual(sorted(target.parent.glob(manifest["tmp_glob"])), [])
            self._check_converges(oracle, home, facts)

    def test_journal_torn_tail_does_not_break_recovery(self) -> None:
        oracle = InvariantChecker(seed=107)
        with tempfile.TemporaryDirectory() as td:
            home, manifest, facts = self._drive("journal-torn-tail", td)
            events = [str(event.get("event", "")) for event in facts["journal_events"]]
            self.assertIn("cp-baseline", events)
            self.assertNotIn("cp-torn", events)
            self.assertEqual(
                _intent_ids(facts),
                [manifest["baseline_effect"]],
            )
            self._check_converges(oracle, home, facts)


if __name__ == "__main__":
    unittest.main()
