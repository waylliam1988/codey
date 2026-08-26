from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.workspace import changes
from tools import live_smoke


class LiveSmokeTests(unittest.TestCase):
    def test_live_smoke_all_defaults_to_web_providers_only(self) -> None:
        self.assertIn("deepseek", live_smoke.PROVIDER_IDS)
        self.assertIn("mimo", live_smoke.PROVIDER_IDS)
        self.assertNotIn("local", live_smoke.PROVIDER_IDS)

    def test_collect_changes_comes_from_changes_module(self) -> None:
        self.assertIs(live_smoke.collect_changes, changes.collect_changes)

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

    def test_references_fixture_requires_reference_update(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live_smoke._make_fixture(root, "references")

            self.assertIn(
                "LIVE_SMOKE_REFERENCE_TARGET",
                (root / "pricing.py").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "discounted_checkout_total",
                (root / "checkout.py").read_text(encoding="utf-8"),
            )
            self.assertFalse(live_smoke._verify_fixture(root, "references")["ok"])

    def test_discussion_fixture_requires_no_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live_smoke._make_fixture(root, "discussion")

            self.assertTrue(live_smoke._verify_fixture(root, "discussion")["ok"])
            (root / "unexpected.txt").write_text("changed", encoding="utf-8")
            self.assertFalse(live_smoke._verify_fixture(root, "discussion")["ok"])

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
            mock.patch.object(live_smoke.provider_controls, "begin_task_context") as begin_context,
            mock.patch.object(live_smoke.provider_controls, "end_task_context") as end_context,
            mock.patch.object(live_smoke, "connect_provider", return_value=provider) as connect,
            mock.patch.object(live_smoke, "run", return_value=mock.Mock(stop_reason="done", summary="done", turns=3)),
            mock.patch.object(live_smoke, "_verify_fixture", return_value={"ok": True, "exit_code": 0, "output": "OK"}),
            mock.patch.object(live_smoke.shutil, "rmtree"),
        ):
            data = live_smoke.run_smoke("deepseek", "create", 9222, 8)

        connect.assert_called_once_with("deepseek", port=9222)
        begin_context.assert_called_once_with("live-smoke:deepseek:create")
        end_context.assert_called_once_with()
        provider.close.assert_called_once_with()
        self.assertTrue(data["ok"])

    def test_run_smoke_cleans_task_context_when_provider_connection_fails(self) -> None:
        with (
            mock.patch.object(live_smoke.provider_controls, "begin_task_context") as begin_context,
            mock.patch.object(live_smoke.provider_controls, "end_task_context") as end_context,
            mock.patch.object(live_smoke, "connect_provider", side_effect=RuntimeError("offline")),
        ):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                live_smoke.run_smoke("qwen", "edit", 9222, 8)

        begin_context.assert_called_once_with("live-smoke:qwen:edit")
        end_context.assert_called_once_with()

    def test_run_smoke_cleans_task_context_when_provider_close_fails(self) -> None:
        provider = mock.Mock()
        provider.close.side_effect = RuntimeError("CDP disconnected")

        with (
            mock.patch.object(live_smoke.provider_controls, "begin_task_context"),
            mock.patch.object(live_smoke.provider_controls, "end_task_context") as end_context,
            mock.patch.object(live_smoke, "connect_provider", return_value=provider),
            mock.patch.object(
                live_smoke,
                "run",
                return_value=mock.Mock(stop_reason="done", summary="done", turns=1),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "CDP disconnected"):
                live_smoke.run_smoke("deepseek", "create", 9222, 8)

        end_context.assert_called_once_with()

    def test_run_smoke_can_use_reviewer(self) -> None:
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        writer.close = mock.Mock()
        reviewer = mock.Mock()
        reviewer.name = "StepFun"
        reviewer.location = "https://chat.stepfun.com/chats/"
        reviewer.send.return_value = '{"verdict":"approved","summary":"Looks good","findings":[]}'
        reviewer.close = mock.Mock()
        changes = {"ok": True, "changed_count": 1, "files": [], "diff": "+x"}

        with (
            mock.patch.object(live_smoke, "connect_provider", side_effect=[writer, reviewer]) as connect,
            mock.patch.object(live_smoke, "run", return_value=mock.Mock(stop_reason="done", summary="done", turns=3)),
            mock.patch.object(live_smoke, "collect_changes", return_value=changes),
            mock.patch.object(live_smoke, "_verify_fixture", return_value={"ok": True, "exit_code": 0, "output": "OK"}),
            mock.patch.object(live_smoke.shutil, "rmtree"),
        ):
            data = live_smoke.run_smoke("deepseek", "edit", 9222, 8, "stepfun")

        self.assertEqual(connect.call_args_list[0], mock.call("deepseek", port=9222))
        self.assertEqual(
            connect.call_args_list[1],
            mock.call("stepfun", port=9222, open_if_missing=False, bring_to_front=False),
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
        reviewer.name = "StepFun"
        reviewer.location = "https://chat.stepfun.com/chats/"
        reviewer.send.side_effect = RuntimeError("generation failed")
        reviewer.close = mock.Mock()
        changes = {"ok": True, "changed_count": 1, "files": [], "diff": "+x"}

        with (
            mock.patch.object(live_smoke, "connect_provider", side_effect=[writer, reviewer]),
            mock.patch.object(live_smoke, "run", return_value=mock.Mock(stop_reason="done", summary="done", turns=3)),
            mock.patch.object(live_smoke, "collect_changes", return_value=changes),
            mock.patch.object(live_smoke, "_verify_fixture", return_value={"ok": True, "exit_code": 0, "output": "OK"}),
            mock.patch.object(live_smoke.shutil, "rmtree"),
        ):
            data = live_smoke.run_smoke("deepseek", "edit", 9222, 8, "stepfun")

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
        reviewer.name = "StepFun"
        reviewer.location = "https://chat.stepfun.com/chats/"
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
            mock.patch.object(live_smoke, "_verify_fixture", return_value={"ok": True, "exit_code": 0, "output": "OK"}),
            mock.patch.object(live_smoke.shutil, "rmtree"),
        ):
            data = live_smoke.run_smoke("deepseek", "edit", 9222, 8, "stepfun")

        self.assertEqual(reviewer.send.call_count, 2)
        self.assertEqual(data["review"], "approved")
        self.assertTrue(data["ok"])

    def test_run_smoke_rejects_same_writer_and_reviewer(self) -> None:
        with self.assertRaisesRegex(ValueError, "reviewer must be different"):
            live_smoke.run_smoke("deepseek", "edit", 9222, 8, "deepseek")

    def test_run_smoke_fails_when_independent_verification_fails(self) -> None:
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.location = "https://chat.deepseek.com/"

        with (
            mock.patch.object(live_smoke, "connect_provider", return_value=provider),
            mock.patch.object(live_smoke, "run", return_value=mock.Mock(stop_reason="done", summary="done", turns=1)),
            mock.patch.object(live_smoke, "_verify_fixture", return_value={"ok": False, "exit_code": 1, "output": "wrong result"}),
        ):
            data = live_smoke.run_smoke("deepseek", "edit", 9222, 8)

        self.assertFalse(data["ok"])
        self.assertEqual(data["verification"]["output"], "wrong result")

    def test_run_matrix_keeps_running_after_provider_failure(self) -> None:
        with mock.patch.object(
            live_smoke,
            "run_smoke",
            side_effect=[{"ok": True, "summary": "done"}, RuntimeError("offline")],
        ):
            data = live_smoke.run_matrix("edit", 9222, 8, ("deepseek", "qwen"))

        self.assertFalse(data["ok"])
        self.assertEqual([item["provider"] for item in data["results"]], ["deepseek", "qwen"])
        self.assertEqual(data["results"][1]["stop_reason"], "error")


if __name__ == "__main__":
    unittest.main()