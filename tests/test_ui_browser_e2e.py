from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.ui_e2e import _save_screenshot, run_ui_e2e


class ScreenshotFailurePage:
    def screenshot(self, **kwargs):
        del kwargs
        raise TimeoutError("capture timed out")


class UiBrowserE2ETests(unittest.TestCase):
    def test_screenshot_artifact_is_best_effort(self) -> None:
        with tempfile.TemporaryDirectory() as artifacts:
            path = Path(artifacts) / "ui-failure.png"
            artifact = _save_screenshot(ScreenshotFailurePage(), path)  # type: ignore[arg-type]

            marker = Path(artifact)
            self.assertEqual(marker.name, "ui-failure.png.txt")
            self.assertTrue(marker.exists())
            self.assertIn("TimeoutError", marker.read_text(encoding="utf-8"))

    def test_complete_project_flow_in_real_edge(self) -> None:
        with tempfile.TemporaryDirectory() as artifacts:
            result = run_ui_e2e(artifacts=artifacts)

        self.assertTrue(result["ok"], result)
        self.assertIn("snapshot restore", result["checks"])
        self.assertIn("shell approval denial", result["checks"])
        self.assertIn("shell approval reconnect recovery", result["checks"])
        self.assertIn("shell approval HTTP reconciliation", result["checks"])
        self.assertIn("shell result snapshot reconciliation", result["checks"])
        self.assertIn("shell result before continued task completion", result["checks"])
        self.assertIn("SSE reconnect reconciliation", result["checks"])
        self.assertIn("stale state cannot override newer SSE completion", result["checks"])
        self.assertIn("run details inline receipt", result["checks"])
        self.assertIn("responsive stop", result["checks"])


if __name__ == "__main__":
    unittest.main()
