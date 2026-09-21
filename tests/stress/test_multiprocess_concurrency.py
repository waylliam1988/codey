"""Level 4: real multiprocess concurrency over shared durable state.

Writers append while a reader canonicalizes while the parent kills
writers with ``Popen.kill()``. Timing is OS-owned, so nothing here
asserts an interleaving: every assertion is a timing-independent durable
property -- prefix-closed ghost logs (no gaps, no torn views), a reader
that never sees corruption or deadlock, a writer that makes progress
under concurrency, and idempotent recovery afterwards.

What this proves beyond Level 3: the file locks serialize cross-process
writers, a kill never leaves a lock held (OS-owned), and concurrent
repair/rebuild paths converge instead of corrupting.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.stress.oracle import InvariantChecker
from tests.stress.process import _child_env, _kill, _recover_canonical

MP_WORKER = Path(__file__).resolve().parent / "mp_worker.py"
PHASE_TIMEOUT = 60.0


def _spawn_mp(*args: str):
    return subprocess.Popen(
        [sys.executable, "-u", str(MP_WORKER), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(),
    )


def _drain_done(proc, want: str) -> dict:
    """Collect stdout lines until the done marker; fail loudly on stderr."""
    import json

    out, err = proc.communicate(timeout=PHASE_TIMEOUT)
    if proc.returncode != 0:
        raise AssertionError(f"mp child failed rc={proc.returncode}: {err[-2000:]}")
    for line in out.strip().splitlines():
        payload = json.loads(line)
        if payload.get("done") == want:
            return payload
    raise AssertionError(f"no done marker for {want!r}: {out[-500:]}")


class MultiprocessConcurrencyTests(unittest.TestCase):
    def test_writers_killed_reader_clean_recovery_converges(self) -> None:
        oracle = InvariantChecker(seed=401)
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "state"
            procs = [
                _spawn_mp("--mode", "write", "--state-home", str(home),
                          "--tag", "mpA", "--rounds", "40", "--session-writes",
                          "--pace", "0.05"),
                _spawn_mp("--mode", "write", "--state-home", str(home),
                          "--tag", "mpB", "--rounds", "40", "--pace", "0.05"),
                _spawn_mp("--mode", "write", "--state-home", str(home),
                          "--tag", "mpC", "--rounds", "40", "--pace", "0.05"),
                _spawn_mp("--mode", "read", "--state-home", str(home), "--reads", "60"),
            ]
            try:
                writers = procs[:3]
                reader = procs[3]
                time.sleep(0.5)
                _kill(writers[1])
                time.sleep(0.5)
                _kill(writers[2])
                # Writer A must make progress despite the kills and the reader.
                done_a = _drain_done(writers[0], "mpA")
                self.assertEqual(done_a["ghost"], 40)
                self.assertEqual(len(done_a["effects"]), 40)
                # The reader must have seen only clean snapshots, then finished.
                done_r = _drain_done(reader, "reader")
                self.assertEqual(done_r["reads"], 60)
            finally:
                for proc in procs:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=PHASE_TIMEOUT)
            facts = _recover_canonical(home)
            oracle.assert_valid(facts)
            second = oracle.check_recovery_idempotent(lambda: _recover_canonical(home))
            self.assertEqual(facts, second)
            # A's completed effects are all durable; ghost tags are prefix-closed.
            intent_ids = {
                str(row.get("effect_id", ""))
                for row in facts["log_rows"] if row.get("record") == "intent"
            }
            self.assertTrue(set(done_a["effects"]) <= intent_ids)
            by_tag: dict[str, list[int]] = {}
            for row in facts["ghost_rows"]:
                tag, _, num = str(row.get("id", "")).rpartition("-")
                by_tag.setdefault(tag, []).append(int(num))
            for tag in ("ghost-mpA", "ghost-mpB", "ghost-mpC"):
                ids = sorted(by_tag.get(tag, []))
                self.assertEqual(ids, list(range(1, len(ids) + 1)), f"gaps in {tag}")


if __name__ == "__main__":
    unittest.main()
