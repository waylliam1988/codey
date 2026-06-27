from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import live_smoke


class LiveSmokeTests(unittest.TestCase):
    def test_create_fixture_starts_empty_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live_smoke._make_fixture(root, "create")

            self.assertEqual(list(root.iterdir()), [])

    def test_edit_fixture_writes_buggy_project_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live_smoke._make_fixture(root, "edit")

            self.assertIn("LIVE_SMOKE_BUG", (root / "pricing.py").read_text(encoding="utf-8"))
            self.assertTrue((root / "test_pricing.py").exists())

    def test_main_supports_json_output_flag(self) -> None:
        with mock.patch.object(live_smoke, "run_smoke", return_value={"ok": True, "summary": "done"}):
            with mock.patch("builtins.print") as print_mock:
                code = live_smoke.main(["--json"])

        self.assertEqual(code, 0)
        self.assertTrue(print_mock.called)

    def test_run_smoke_calls_provider_with_requested_port(self) -> None:
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.location = "https://chat.deepseek.com/"
        provider.close = mock.Mock()

        with (
            mock.patch.object(live_smoke, "connect_provider", return_value=provider) as connect,
            mock.patch.object(live_smoke, "run", return_value=mock.Mock(stop_reason="done", summary="done", turns=3)),
            mock.patch.object(live_smoke.shutil, "rmtree"),
        ):
            data = live_smoke.run_smoke("deepseek", "create", 9222, 8)

        connect.assert_called_once_with("deepseek", port=9222)
        provider.close.assert_called_once_with()
        self.assertTrue(data["ok"])


if __name__ == "__main__":
    unittest.main()
