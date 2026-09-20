"""Server import must not construct the global AppContext or load Playwright."""

from __future__ import annotations

import subprocess
import sys
import unittest
from unittest import mock

_HEAVY_MODULES = (
    "codey.automation.browser",
    "codey.providers.registry",
    "codey.providers.web_provider",
    "codey.providers.worker",
    "codey.research.runner",
    "codey.research.browser_search",
)


def _heavy_after_import(dotted: str) -> list[str]:
    script = (
        f"import sys, {dotted}; "
        "heavy = sorted("
        "m for m in sys.modules "
        "if m == 'playwright' or m.startswith('playwright.') "
        f"or m in {_HEAVY_MODULES!r}); "
        "print(','.join(heavy))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    return [name for name in completed.stdout.strip().split(",") if name]


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

    def test_server_import_loads_no_browser_stack(self) -> None:
        self.assertEqual(_heavy_after_import("codey.app.server"), [])

    def test_api_import_loads_no_browser_stack(self) -> None:
        self.assertEqual(_heavy_after_import("codey.app.api"), [])

    def test_context_import_loads_no_browser_stack(self) -> None:
        self.assertEqual(_heavy_after_import("codey.app.context"), [])

    def test_services_import_loads_no_browser_stack(self) -> None:
        self.assertEqual(_heavy_after_import("codey.app.services"), [])

    def test_get_state_honors_patched_global(self) -> None:
        from codey.app import server

        sentinel = object()
        with mock.patch.object(server, "STATE", sentinel):
            self.assertIs(server.get_state(), sentinel)


if __name__ == "__main__":
    unittest.main()
