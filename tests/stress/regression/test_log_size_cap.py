"""The session spine cap fails loud, never silent.

Found by the nightly soak at step 5851: one session log caps at 4 MB of
compacted spine and further writes raise ``RuntimeLogWriteError``. That is
production backpressure by design, and this test pins the contract at
small scale: the write raises (no partial commit, no silent loss), prior
facts stay intact, and rereading is stable.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.runtime.log.entries import RuntimeLogWriteError
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.mutation_line import RuntimeMutationLine

SESSION = "cap-session"


class LogSizeCapTests(unittest.TestCase):
    def test_past_cap_raises_and_keeps_prior_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td) / "state", max_log_bytes=8192)
            line = RuntimeMutationLine(log)
            runs = 0
            with self.assertRaises(RuntimeLogWriteError):
                for index in range(10_000):
                    run_id = f"cap-{index:05d}"
                    line.accept_operation(
                        session_id=SESSION,
                        run_id=run_id,
                        project="stress-project",
                        provider_id="mock",
                        turn_budget=10,
                        max_repair_rounds=1,
                    )
                    runs += 1
            self.assertGreater(runs, 0)
            first = log.read(SESSION)
            self.assertGreater(len(first), 0)
            # Nothing half-committed: every row parses and rereads identically.
            self.assertEqual(list(log.read(SESSION)), list(first))


if __name__ == "__main__":
    unittest.main()
