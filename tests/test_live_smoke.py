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

    def test_run_smoke_can_use_reviewer(self) -> None:
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        writer.close = mock.Mock()
        reviewer = mock.Mock()
        reviewer.name = "MiMo"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        reviewer.send.return_value = '{"verdict":"approved","summary":"Looks good","findings":[]}'
        reviewer.close = mock.Mock()
        changes = {"ok": True, "changed_count": 1, "files": [], "diff": "+x"}

        with (
            mock.patch.object(live_smoke, "connect_provider", side_effect=[writer, reviewer]) as connect,
            mock.patch.object(live_smoke, "run", return_value=mock.Mock(stop_reason="done", summary="done", turns=3)),
            mock.patch.object(live_smoke, "collect_changes", return_value=changes),
            mock.patch.object(live_smoke.shutil, "rmtree"),
        ):
            data = live_smoke.run_smoke("deepseek", "edit", 9222, 8, "mimo")

        self.assertEqual(connect.call_args_list[0], mock.call("deepseek", port=9222))
        self.assertEqual(
            connect.call_args_list[1],
            mock.call("mimo", port=9222, open_if_missing=False, bring_to_front=False),
        )
        reviewer.new_chat.assert_called_once_with()
        reviewer.close.assert_called_once_with()
        self.assertEqual(data["review"], "approved")
        self.assertTrue(data["ok"])

    def test_run_smoke_reports_reviewer_failure_without_failing_task(self) -> None:
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        writer.close = mock.Mock()
        reviewer = mock.Mock()
        reviewer.name = "MiMo"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        reviewer.send.side_effect = RuntimeError("generation failed")
        reviewer.close = mock.Mock()
        changes = {"ok": True, "changed_count": 1, "files": [], "diff": "+x"}

        with (
            mock.patch.object(live_smoke, "connect_provider", side_effect=[writer, reviewer]),
            mock.patch.object(live_smoke, "run", return_value=mock.Mock(stop_reason="done", summary="done", turns=3)),
            mock.patch.object(live_smoke, "collect_changes", return_value=changes),
            mock.patch.object(live_smoke.shutil, "rmtree"),
        ):
            data = live_smoke.run_smoke("deepseek", "edit", 9222, 8, "mimo")

        reviewer.close.assert_called_once_with()
        self.assertEqual(data["review"], "unavailable")
        self.assertTrue(data["ok"])
        self.assertTrue(any("generation failed" in event for event in data["events"]))

    def test_run_smoke_repairs_invalid_reviewer_json_once(self) -> None:
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        writer.close = mock.Mock()
        reviewer = mock.Mock()
        reviewer.name = "MiMo"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        reviewer.send.side_effect = [
            "looks good but not json",
            '{"verdict":"approved","summary":"Looks good","findings":[]}',
        ]
        reviewer.close = mock.Mock()
        changes = {"ok": True, "changed_count": 1, "files": [], "diff": "+x"}

        with (
            mock.patch.object(live_smoke, "connect_provider", side_effect=[writer, reviewer]),
            mock.patch.object(live_smoke, "run", return_value=mock.Mock(stop_reason="done", summary="done", turns=3)),
            mock.patch.object(live_smoke, "collect_changes", return_value=changes),
            mock.patch.object(live_smoke.shutil, "rmtree"),
        ):
            data = live_smoke.run_smoke("deepseek", "edit", 9222, 8, "mimo")

        self.assertEqual(reviewer.send.call_count, 2)
        self.assertEqual(data["review"], "approved")
        self.assertTrue(data["ok"])

    def test_run_smoke_rejects_same_writer_and_reviewer(self) -> None:
        with self.assertRaisesRegex(ValueError, "reviewer must be different"):
            live_smoke.run_smoke("deepseek", "edit", 9222, 8, "deepseek")


if __name__ == "__main__":
    unittest.main()
