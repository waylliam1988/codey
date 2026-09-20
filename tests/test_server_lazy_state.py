"""Server import must not construct the global AppContext."""

from __future__ import annotations

import subprocess
import sys
import unittest
from unittest import mock


class ServerLazyStateTests(unittest.TestCase):
    def test_import_leaves_global_unbuilt(self) -> None:
        script = (
            "import codey.app.server as server; "
            "print('built' if server.STATE is not None else 'lazy')"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "lazy")

    def test_get_state_honors_patched_global(self) -> None:
        from codey.app import server

        sentinel = object()
        with mock.patch.object(server, "STATE", sentinel):
            self.assertIs(server.get_state(), sentinel)


if __name__ == "__main__":
    unittest.main()
