"""P2: self-repair crash -- journal recovery with one logical repair.

Honest semantics of the current supervisor (asserted here, not wished):

- In-process: a crashing runner keeps the job queued (retry), and the
  enqueue cooldown dedupes repeat signals. One logical repair.
- Across restart: the queue and cooldown are memory-only and do NOT
  survive. What survives is the journal: queued/runner_error rows without
  a matching finished row make the unfinished job DETECTABLE, and the
  logical identity (provider, kind, stage) stays single across attempts.

Automatic journal replay on boot does not exist yet; see the batch summary.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.providers.diagnostics import ProviderFailure
from codey.providers.supervisor import STATE_OPEN, ProviderHealth
from codey.repairs.self_repair import SelfRepairSupervisor
from tests.stress.oracle import InvariantChecker


def _failure() -> ProviderFailure:
    return ProviderFailure(
        "Qwen", "send", "", "", "response_missing", "now", "response_missing"
    )


def _journal_rows(state_home: Path) -> list[dict]:
    path = state_home / "self-repair" / "journal.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _logical_key(row: dict) -> tuple:
    return (row.get("provider"), row.get("failure_kind"), row.get("failure_stage"))


def _unfinished_queued(rows: list[dict]) -> list[dict]:
    # Finished rows carry no kind/stage, so unfinished is matched by
    # provider: a provider with attempts but no finish is still open.
    finished = {
        row.get("provider") for row in rows if row.get("event") == "self_repair_finished"
    }
    return [
        row
        for row in rows
        if row.get("event") in {"self_repair_queued", "self_repair_runner_error"}
        and row.get("provider") not in finished
    ]


class SelfRepairCrashTests(unittest.TestCase):
    def test_crashed_repair_stays_queued_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            crashed: list[str] = []

            def _crashing_runner(job):
                crashed.append(job.provider_id)
                raise RuntimeError("process kill simulation")

            supervisor = SelfRepairSupervisor(home, runner=_crashing_runner)
            supervisor.maybe_enqueue(
                "qwen", _failure(), ProviderHealth(state=STATE_OPEN, last_failure_kind="x")
            )
            supervisor.run_pending_once()
            self.assertEqual(crashed, ["qwen"])
            # Still queued: the crash did not lose the job in-process.
            self.assertEqual(len(supervisor.pending()), 1)

    def test_repeat_signal_dedupes_to_one_logical_repair(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            supervisor = SelfRepairSupervisor(home, runner=mock.Mock())
            first = supervisor.maybe_enqueue(
                "qwen", _failure(), ProviderHealth(state=STATE_OPEN, last_failure_kind="x")
            )
            second = supervisor.maybe_enqueue(
                "qwen", _failure(), ProviderHealth(state=STATE_OPEN, last_failure_kind="x")
            )
            self.assertTrue(first)
            self.assertFalse(second)
            rows = _journal_rows(home)
            logical = {_logical_key(row) for row in rows if row.get("event") == "self_repair_queued"}
            self.assertEqual(len(logical), 1)

    def test_unfinished_job_detectable_from_journal_after_restart(self) -> None:
        oracle = InvariantChecker(seed=22)
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)

            def _crashing_runner(job):
                raise RuntimeError("process kill simulation")

            supervisor = SelfRepairSupervisor(home, runner=_crashing_runner)
            supervisor.maybe_enqueue(
                "qwen", _failure(), ProviderHealth(state=STATE_OPEN, last_failure_kind="x")
            )
            supervisor.run_pending_once()
            # "Restart": only bytes on disk survive.
            rows = _journal_rows(home)
            unfinished = _unfinished_queued(rows)
            self.assertEqual(len(unfinished), 2)
            self.assertEqual(
                {row["event"] for row in unfinished},
                {"self_repair_queued", "self_repair_runner_error"},
            )
            # Retry under the same logical identity finishes it.
            done: list[str] = []
            retry = SelfRepairSupervisor(
                home, runner=lambda job: done.append(job.provider_id) or mock.Mock(ok=True)
            )
            retry.maybe_enqueue(
                "qwen", _failure(), ProviderHealth(state=STATE_OPEN, last_failure_kind="x")
            )
            retry.run_pending_once()
            self.assertEqual(done, ["qwen"])
            rows = _journal_rows(home)
            logical = {_logical_key(row) for row in rows if row.get("event") == "self_repair_queued"}
            self.assertEqual(len(logical), 1)
            self.assertEqual(_unfinished_queued(rows), [])
            oracle.assert_valid({
                "log_rows": [], "ghost_rows": [], "delivery_batches": [],
                "approvals": [], "journal_events": [],
            })


if __name__ == "__main__":
    unittest.main()
