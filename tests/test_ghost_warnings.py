from __future__ import annotations

import unittest

from codey.ghost import affinity, continuity, directive, sleep, work_queue
from codey.ghost._warnings import (
    bounded_warnings,
)
from codey.ghost.affinity_model import MAX_AFFINITY_WARNINGS
from codey.ghost.work_queue_model import MAX_WORK_WARNINGS


class GhostWarningsEquivalenceTests(unittest.TestCase):
    def test_bounded_helpers_match_shared_loop(self) -> None:
        sample = ["b", "a", "b", "", "a", "c"]
        self.assertEqual(
            work_queue._bounded_warnings(sample),
            bounded_warnings(sample, limit=MAX_WORK_WARNINGS, redact_sensitive=True),
        )
        self.assertEqual(
            affinity._bounded_warnings(sample),
            bounded_warnings(sample, limit=MAX_AFFINITY_WARNINGS),
        )
        self.assertEqual(
            continuity._bounded_warnings(sample),
            bounded_warnings(sample, limit=continuity.MAX_CONTINUITY_WARNINGS),
        )
        self.assertEqual(
            directive._bounded_warnings(list(sample)),
            bounded_warnings(sample, limit=directive.MAX_DIRECTIVE_WARNINGS),
        )
        self.assertEqual(
            sleep._bounded_warnings(sample),
            bounded_warnings(sample, limit=sleep.MAX_SLEEP_WARNINGS, redact_sensitive=True),
        )



if __name__ == "__main__":
    unittest.main()
